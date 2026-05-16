"""Memory system Tier-1 tests (spec v3).

Covers Changes 1-3:
 - Embedding model versioning + reindex endpoint
 - Pinned flag, PATCH coercion (case-insensitive), pin-limit at PATCH
 - MD5 content-hash dedup at write time + PATCH content-collision 409
 - Atomic backfill of content_hash on init_db

Run: pytest tests/test_memory_v3.py -q
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CSRF_HEADER = {"X-Requested-With": "lm-chat"}


def _post(url: str, body: dict, method: str = "POST"):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **CSRF_HEADER},
        method=method,
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


def _get(url: str):
    req = urllib.request.Request(url, headers=CSRF_HEADER)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


def _md5_norm(content: str) -> str:
    norm = " ".join((content or "").lower().split())
    return hashlib.md5(norm.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Module-level imports of server (for backfill unit test)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def server_module():
    """Import server.py as a module so we can call helper functions
    directly without going through HTTP.  Path resolved relative to
    this test file.
    """
    repo_root = Path(__file__).parent.parent
    sys.path.insert(0, str(repo_root))
    import server as srv
    return srv


# ---------------------------------------------------------------------------
# Change 3 — content_hash dedup
# ---------------------------------------------------------------------------

class TestContentHashDedup:
    """MD5 content-hash dedup at write time prevents byte-identical
    re-distillation."""

    def test_module_helper_normalizes_whitespace_and_case(self, server_module):
        srv = server_module
        # Same hash for whitespace / case variants
        h1 = srv._content_hash_text("Hello world")
        h2 = srv._content_hash_text("hello  world")
        h3 = srv._content_hash_text("HELLO WORLD")
        h4 = srv._content_hash_text("  hello\tworld  ")
        assert h1 == h2 == h3 == h4
        # Different content -> different hash
        assert h1 != srv._content_hash_text("hello there")

    def test_backfill_dedupes_existing_duplicates(self, server_module, tmp_path):
        srv = server_module
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(db_path)
        try:
            srv._apply_pragmas(conn)
            srv._create_schema(conn)
            srv._run_migrations(conn)
            srv._seed_default_user(conn)
            # Seed three identical-content rows with different created_at.
            now = time.time()
            for i, ts in enumerate([now - 100, now - 50, now]):
                conn.execute(
                    "INSERT INTO user_insights (id, user_id, content, category, created_at, last_used) "
                    "VALUES (?, 'default', 'user likes python', 'preference', ?, ?)",
                    (f"id{i}", ts, ts),
                )
            conn.commit()
            # Run backfill — should collapse to 1 (the oldest).
            srv._backfill_content_hashes(conn)
            rows = conn.execute(
                "SELECT id, content_hash FROM user_insights WHERE user_id='default'"
            ).fetchall()
            assert len(rows) == 1, f"expected 1 row after dedup, got {len(rows)}"
            assert rows[0][1] == _md5_norm("user likes python")
            # The surviving row should be id0 (oldest created_at).
            assert rows[0][0] == "id0"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Change 2 — pinned flag + PATCH coercion + pin-limit
# ---------------------------------------------------------------------------

class TestPinnedFlag:
    """Pinned insights inject unconditionally up to _PINNED_INSIGHTS_LIMIT."""

    def test_pin_via_patch_then_unpin(self, app_server: str):
        # Add an insight
        code, body = _post(f"{app_server}/api/insights",
                           {"content": "user prefers concise replies", "category": "preference"})
        assert code == 201, body
        insight_id = body["id"]
        # Pin it via boolean true
        code, _ = _post(f"{app_server}/api/insights/{insight_id}/edit",
                        {"pinned": True})
        assert code == 200
        code, listed = _get(f"{app_server}/api/insights")
        target = [i for i in listed if i["id"] == insight_id][0]
        assert target["pinned"] == 1
        # Unpin via int 0
        code, _ = _post(f"{app_server}/api/insights/{insight_id}/edit",
                        {"pinned": 0})
        assert code == 200
        code, listed = _get(f"{app_server}/api/insights")
        target = [i for i in listed if i["id"] == insight_id][0]
        assert target["pinned"] == 0

    @pytest.mark.parametrize("value,expected_pinned", [
        ("1", 1), ("true", 1), ("True", 1), ("TRUE", 1), ("YES", 1),
        ("0", 0), ("false", 0), ("False", 0), ("FALSE", 0), ("no", 0),
    ])
    def test_pinned_coercion_case_insensitive(self, app_server, value, expected_pinned):
        """N5 fix — minimax flagged: `"TRUE"` was silently coerced to 0
        because string match was case-sensitive."""
        code, body = _post(f"{app_server}/api/insights",
                           {"content": f"insight {value}", "category": "context"})
        assert code == 201
        insight_id = body["id"]
        code, resp = _post(f"{app_server}/api/insights/{insight_id}/edit",
                           {"pinned": value})
        assert code == 200, resp
        code, listed = _get(f"{app_server}/api/insights")
        target = [i for i in listed if i["id"] == insight_id][0]
        assert target["pinned"] == expected_pinned, \
            f"value={value!r} expected pinned={expected_pinned} got {target['pinned']}"

    def test_pinned_coercion_rejects_garbage(self, app_server):
        code, body = _post(f"{app_server}/api/insights",
                           {"content": "another insight", "category": "context"})
        assert code == 201
        insight_id = body["id"]
        code, resp = _post(f"{app_server}/api/insights/{insight_id}/edit",
                           {"pinned": "garbage"})
        assert code == 400, resp

    def test_pin_limit_enforced(self, app_server: str):
        """5 hard limit on pinned-active count per user."""
        ids = []
        for i in range(6):
            code, body = _post(f"{app_server}/api/insights",
                               {"content": f"pinnable insight number {i}",
                                "category": "context"})
            assert code == 201
            ids.append(body["id"])
        # Pin first 5 — succeed
        for iid in ids[:5]:
            code, _ = _post(f"{app_server}/api/insights/{iid}/edit",
                            {"pinned": 1})
            assert code == 200
        # 6th — 409
        code, resp = _post(f"{app_server}/api/insights/{ids[5]}/edit",
                           {"pinned": 1})
        assert code == 409, resp
        assert resp.get("error", {}).get("error") == "pin_limit" or "pin_limit" in str(resp)
        # Unpin one, retry — succeed
        code, _ = _post(f"{app_server}/api/insights/{ids[0]}/edit",
                        {"pinned": 0})
        assert code == 200
        code, _ = _post(f"{app_server}/api/insights/{ids[5]}/edit",
                        {"pinned": 1})
        assert code == 200


# ---------------------------------------------------------------------------
# Change 3 — PATCH content collision
# ---------------------------------------------------------------------------

class TestPatchContentCollision:
    def test_patch_content_to_existing_hash_returns_409(self, app_server: str):
        code, body_a = _post(f"{app_server}/api/insights",
                             {"content": "user likes vim", "category": "preference"})
        assert code == 201
        code, body_b = _post(f"{app_server}/api/insights",
                             {"content": "user likes emacs", "category": "preference"})
        assert code == 201
        # Try to edit B to have A's content — 409
        code, resp = _post(f"{app_server}/api/insights/{body_b['id']}/edit",
                           {"content": "user likes vim"})
        assert code == 409, resp


# ---------------------------------------------------------------------------
# Change 1 — embedding model id + reindex endpoint
# ---------------------------------------------------------------------------

class TestEmbeddingModelVersioning:
    """Reindex returns 503 when no embedding model loaded."""

    def test_reindex_returns_503_when_no_embedding_model(self, app_server: str):
        # Default mock LM Studio doesn't include an embedding model with
        # loaded_instances, so the helper returns None.
        code, resp = _post(f"{app_server}/api/insights/reindex", {})
        assert code == 503, resp
