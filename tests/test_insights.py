"""
Tests for the insights (adaptive memory) API.

Routes covered:
  GET    /api/insights
  POST   /api/insights
  POST   /api/insights/{id}/edit
  DELETE /api/insights/{id}
  DELETE /api/insights
  POST   /api/insights/distill
  POST   /api/insights/refine

Key implementation notes (verified against server.py):
  - Field name is "content" (not "text")
  - _add_insight returns 201 on success
  - GET /api/insights returns a plain JSON list, not {"insights": [...]}
  - Invalid category silently defaults to "context" (no 400)
  - _refine_insights returns 200 with a message when < 3 insights (no LLM call)
  - VALID_INSIGHT_CATEGORIES = {"identity", "preference", "skill", "project", "opinion", "context"}
"""

import json, time, urllib.error

import pytest

from conftest import _Client, _create_chat


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _add_insight(client: _Client, content: str = "I prefer Python", category: str = "preference") -> dict:
    resp = client.post("/api/insights", {"content": content, "category": category})
    return json.loads(resp.read())


def _list_insights(client: _Client) -> list:
    resp = client.get("/api/insights")
    data = json.loads(resp.read())
    # Server returns a plain list directly
    if isinstance(data, list):
        return data
    return data.get("insights", [])


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

class TestInsightsList:
    def test_insights_list_empty_by_default(self, client):
        insights = _list_insights(client)
        assert insights == []

    def test_insights_list_returns_after_add(self, client):
        _add_insight(client)
        insights = _list_insights(client)
        assert len(insights) == 1
        assert insights[0]["content"] == "I prefer Python"

    def test_insights_list_contains_category(self, client):
        _add_insight(client, content="I am a developer", category="identity")
        insights = _list_insights(client)
        assert any(i["category"] == "identity" for i in insights)

    def test_insights_list_isolated_per_user(self, authed_client):
        # Admin adds insight
        _add_insight(authed_client.admin, "Admin only insight")
        # testuser sees none
        user_insights = _list_insights(authed_client.user)
        assert all(i["content"] != "Admin only insight" for i in user_insights)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

class TestInsightsCrud:
    def test_add_insight_returns_201(self, client):
        resp = client.post("/api/insights", {"content": "I like dark mode", "category": "preference"})
        assert resp.status == 201

    def test_add_insight_missing_content_returns_400(self, client):
        with pytest.raises(urllib.error.HTTPError) as exc:
            client.post("/api/insights", {"category": "context"})
        assert exc.value.code == 400

    def test_add_insight_invalid_category_defaults_to_context(self, client):
        # Invalid categories silently default to "context" rather than returning 400
        resp = client.post("/api/insights", {"content": "test", "category": "invalidcategory"})
        assert resp.status == 201
        data = json.loads(resp.read())
        assert data["category"] == "context"

    def test_edit_insight_updates_content(self, client):
        insight = _add_insight(client, content="Old text", category="context")
        insight_id = insight["id"]
        client.post(f"/api/insights/{insight_id}/edit", {"content": "New text", "category": "context"})
        insights = _list_insights(client)
        updated = next(i for i in insights if i["id"] == insight_id)
        assert updated["content"] == "New text"

    def test_edit_insight_no_fields_returns_400(self, client):
        # Providing no recognized fields ("content" or "category") → 400 "nothing to update"
        insight = _add_insight(client)
        with pytest.raises(urllib.error.HTTPError) as exc:
            client.post(f"/api/insights/{insight['id']}/edit", {})
        assert exc.value.code == 400

    def test_edit_nonexistent_insight_returns_404(self, client):
        with pytest.raises(urllib.error.HTTPError) as exc:
            client.post("/api/insights/nonexistent-id-xyz/edit", {"content": "hi", "category": "context"})
        assert exc.value.code == 404

    def test_delete_insight_removes_it(self, client):
        insight = _add_insight(client, content="To be deleted")
        client.delete(f"/api/insights/{insight['id']}")
        insights = _list_insights(client)
        assert all(i["id"] != insight["id"] for i in insights)

    def test_delete_nonexistent_insight_returns_404(self, client):
        with pytest.raises(urllib.error.HTTPError) as exc:
            client.delete("/api/insights/nonexistent-xyz")
        assert exc.value.code == 404

    def test_delete_all_insights_clears_list(self, client):
        _add_insight(client, content="First")
        _add_insight(client, content="Second")
        client.delete("/api/insights")
        assert _list_insights(client) == []

    def test_delete_other_users_insight_returns_403_or_404(self, authed_client):
        insight = _add_insight(authed_client.admin, "Admin insight")
        with pytest.raises(urllib.error.HTTPError) as exc:
            authed_client.user.delete(f"/api/insights/{insight['id']}")
        assert exc.value.code in (403, 404)


# ---------------------------------------------------------------------------
# Distill + Refine (LLM calls)
# ---------------------------------------------------------------------------

class TestInsightDistillRefine:
    def test_distill_makes_upstream_llm_call(self, client, mock_lmstudio, app_server):
        # Configure mock to return a non-streaming response
        mock_lmstudio.configure(chunks=[], validate_schema=False)
        chat_id = _create_chat(client)
        # Seed a message (streaming)
        client.post("/api/chat", {
            "model": "test-model",
            "input": "I really love using Python for everything",
            "chat_id": chat_id,
        })
        time.sleep(0.2)
        initial_count = mock_lmstudio.call_count
        resp = client.post("/api/insights/distill", {"chat_id": chat_id, "model": "test-model"})
        assert resp.status == 200
        # Server should have made at least one more LLM call for distillation
        assert mock_lmstudio.call_count > initial_count

    def test_distill_nonexistent_chat_returns_404(self, authed_client):
        # With auth enabled, _verify_chat_owner enforces chat ownership.
        # With AUTH=false it always returns True, so we need an authed context.
        with pytest.raises(urllib.error.HTTPError) as exc:
            authed_client.admin.post("/api/insights/distill", {"chat_id": "nonexistent-chat-id-xyz"})
        assert exc.value.code == 404

    def test_distill_without_chat_id_returns_400(self, client):
        with pytest.raises(urllib.error.HTTPError) as exc:
            client.post("/api/insights/distill", {})
        assert exc.value.code == 400

    def test_refine_with_too_few_insights_returns_200_noop(self, client, mock_lmstudio):
        # No insights seeded — refine returns 200 immediately without LLM call (< 3 insights)
        mock_lmstudio.configure(chunks=[], validate_schema=False)
        resp = client.post("/api/insights/refine", {})
        assert resp.status == 200
        data = json.loads(resp.read())
        # Should return a "too few insights" message, not an error
        assert "message" in data or "count" in data

    def test_refine_empty_insights_does_not_call_llm(self, client, mock_lmstudio):
        # With 0 insights, refine should not make any LLM call
        mock_lmstudio.configure(chunks=[], validate_schema=False)
        initial_count = mock_lmstudio.call_count
        client.post("/api/insights/refine", {})
        # LLM should NOT have been called (too few insights to refine)
        assert mock_lmstudio.call_count == initial_count


import urllib.request as _urllib_request


def _stream_chat(base_url: str, body: dict, cookie: str = "") -> None:
    """POST to /api/chat/stream and consume the full response."""
    data = json.dumps(body).encode()
    req = _urllib_request.Request(
        base_url + "/api/chat/stream",
        data=data,
        headers={"Content-Type": "application/json", "X-Requested-With": "lm-chat"},
        method="POST",
    )
    if cookie:
        req.add_header("Cookie", cookie)
    resp = _urllib_request.urlopen(req, timeout=15)
    resp.read()


# ---------------------------------------------------------------------------
# Insight settings
# ---------------------------------------------------------------------------

class TestInsightSettings:
    def test_get_settings_returns_defaults(self, client):
        resp = client.get("/api/insights/settings")
        data = json.loads(resp.read())
        assert "memory_enabled" in data
        assert "memory_max_inject" in data

    def test_save_memory_enabled_false(self, client):
        client.patch("/api/insights/settings", {"memory_enabled": "false"})
        resp = client.get("/api/insights/settings")
        data = json.loads(resp.read())
        assert data["memory_enabled"] == "false"

    def test_save_memory_enabled_restores_to_true(self, client):
        client.patch("/api/insights/settings", {"memory_enabled": "false"})
        client.patch("/api/insights/settings", {"memory_enabled": "true"})
        resp = client.get("/api/insights/settings")
        data = json.loads(resp.read())
        assert data["memory_enabled"] == "true"

    def test_save_memory_max_inject_valid(self, client):
        client.patch("/api/insights/settings", {"memory_max_inject": "5"})
        resp = client.get("/api/insights/settings")
        data = json.loads(resp.read())
        assert data["memory_max_inject"] == "5"

    def test_save_memory_max_inject_invalid_silently_ignored(self, client):
        # Invalid (non-numeric) memory_max_inject is silently ignored — setting unchanged
        client.patch("/api/insights/settings", {"memory_max_inject": "5"})
        resp = client.patch("/api/insights/settings", {"memory_max_inject": "not-a-number"})
        assert resp.status == 200
        settings = json.loads(client.get("/api/insights/settings").read())
        # Previous valid value is preserved
        assert settings["memory_max_inject"] == "5"

    def test_insight_settings_isolated_per_user(self, authed_client):
        authed_client.admin.patch("/api/insights/settings", {"memory_enabled": "false"})
        resp = authed_client.user.get("/api/insights/settings")
        data = json.loads(resp.read())
        # testuser still has default (true)
        assert data["memory_enabled"] != "false"


# ---------------------------------------------------------------------------
# Memory injection into upstream LLM request
# ---------------------------------------------------------------------------

class TestMemoryInjection:
    def test_memory_injected_into_upstream_request(self, client, app_server, mock_lmstudio):
        """After seeding an insight, the upstream LLM request must contain it
        in the ``system_prompt`` field specifically — not just anywhere in the
        request JSON.  Previously this test serialised the whole request and
        used substring matching, so the insight text could land in any field
        (e.g. echoed via a debug dump) and the test would still pass.
        """
        unique_marker = "user always writes Python-marker-x42"
        client.post("/api/insights", {"content": unique_marker, "category": "skill"})
        mock_lmstudio.configure(chunks=["OK"])
        _stream_chat(app_server, {"model": "test-model", "input": "Hello"}, cookie=client.cookie)
        time.sleep(0.2)
        req = mock_lmstudio.last_request or {}
        system_prompt = req.get("system_prompt") or ""
        assert unique_marker in system_prompt, (
            f"insight not injected into system_prompt; full request: {req!r}"
        )

    def test_memory_not_injected_when_disabled(self, client, app_server, mock_lmstudio):
        client.post("/api/insights", {"content": "Secret insight must not appear", "category": "context"})
        client.patch("/api/insights/settings", {"memory_enabled": "false"})
        mock_lmstudio.configure(chunks=["OK"])
        _stream_chat(app_server, {"model": "test-model", "input": "Hello"}, cookie=client.cookie)
        time.sleep(0.2)
        req_str = json.dumps(mock_lmstudio.last_request)
        assert "Secret insight must not appear" not in req_str

    def test_memory_not_injected_in_incognito(self, client, app_server, mock_lmstudio):
        client.post("/api/insights", {"content": "Private insight not for incognito", "category": "context"})
        mock_lmstudio.configure(chunks=["OK"])
        _stream_chat(app_server, {
            "model": "test-model", "input": "Hello", "incognito": True,
        }, cookie=client.cookie)
        time.sleep(0.2)
        req_str = json.dumps(mock_lmstudio.last_request)
        assert "Private insight not for incognito" not in req_str

    def test_memory_max_inject_limits_count(self, client, app_server, mock_lmstudio):
        """With max_inject=1 and 3 insights, at most 1 insight text appears in upstream request."""
        client.patch("/api/insights/settings", {"memory_max_inject": "1"})
        texts = ["Alpha insight", "Beta insight", "Gamma insight"]
        for t in texts:
            client.post("/api/insights", {"content": t, "category": "context"})
        mock_lmstudio.configure(chunks=["OK"])
        _stream_chat(app_server, {"model": "test-model", "input": "Hello"}, cookie=client.cookie)
        time.sleep(0.2)
        req_str = json.dumps(mock_lmstudio.last_request)
        found = sum(1 for t in texts if t in req_str)
        assert found <= 1

    def test_cross_user_insight_not_in_other_users_request(self, authed_client, mock_lmstudio):
        """Admin's insights must not appear in testuser's upstream requests."""
        authed_client.admin.post("/api/insights", {"content": "Admin-only knowledge XYZ", "category": "context"})
        mock_lmstudio.configure(chunks=["OK"])
        base = authed_client.user.base_url
        data = json.dumps({"model": "test-model", "input": "Hello"}).encode()
        req = _urllib_request.Request(
            base + "/api/chat/stream", data=data,
            headers={"Content-Type": "application/json", "X-Requested-With": "lm-chat"},
            method="POST",
        )
        req.add_header("Cookie", authed_client.user.cookie)
        _urllib_request.urlopen(req, timeout=15).read()
        time.sleep(0.2)
        req_str = json.dumps(mock_lmstudio.last_request)
        assert "Admin-only knowledge XYZ" not in req_str
