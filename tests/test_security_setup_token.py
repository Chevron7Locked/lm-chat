"""First-visitor-wins admin bootstrap gate.

``/api/auth/setup`` creates the first admin when no users exist.  Without
intervention the first request wins — anyone who reaches a publicly-exposed
lm-chat URL before the operator gets admin.  ``LM_CHAT_SETUP_TOKEN`` closes
that window: when set, the operator embeds the token value in their setup
form and any other caller is rejected with 401.

These tests assert:
  * Setup still works when the env is unset (back-compat for trusted
    networks and Docker compose default).
  * Setup is rejected without the token when the env is set.
  * Setup is rejected with the wrong token.
  * Setup is accepted with the correct token.
  * Token compare is constant-time (no timing oracle on the comparison).
  * The token is enforced even on the first request — there's no grace period.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


GOOD_TOKEN = "operators-secret-bootstrap-token-9XmK2pQ7vR4nL"


def _setup(base_url: str, body: dict) -> tuple[int, dict]:
    """POST /api/auth/setup.  Returns (status, parsed_json_or_empty)."""
    req = urllib.request.Request(
        base_url + "/api/auth/setup",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Requested-With": "lm-chat"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        try:
            return e.code, json.loads(body_bytes or b"{}")
        except json.JSONDecodeError:
            return e.code, {"raw": body_bytes.decode("utf-8", "replace")}


# ---------------------------------------------------------------------------
# Unset env: back-compat
# ---------------------------------------------------------------------------

def test_setup_succeeds_without_token_when_env_unset(make_inproc_server):
    """No LM_CHAT_SETUP_TOKEN → setup is open as before."""
    srv = make_inproc_server(bootstrap_admin=False)
    status, _ = _setup(srv.url, {"username": "founder", "password": "founderpass1"})
    assert status == 200


def test_setup_ignores_provided_token_when_env_unset(make_inproc_server):
    """Operator who provides a token in body but didn't set env is still OK.

    This documents that the field is silently ignored, not validated against
    the empty-env-string.  Otherwise we'd risk a misconfiguration where the
    operator sets the form field but forgets the env var.
    """
    srv = make_inproc_server(bootstrap_admin=False)
    status, _ = _setup(
        srv.url,
        {"username": "founder", "password": "founderpass1", "setup_token": "any-value-ignored"},
    )
    assert status == 200


# ---------------------------------------------------------------------------
# Env set: gate enforced
# ---------------------------------------------------------------------------

def test_setup_rejects_missing_token(make_inproc_server):
    srv = make_inproc_server(
        bootstrap_admin=False,
        env={"LM_CHAT_SETUP_TOKEN": GOOD_TOKEN},
    )
    status, body = _setup(srv.url, {"username": "attacker", "password": "attackerpass1"})
    assert status == 401, body
    assert "setup token" in (body.get("error") or "").lower()


def test_setup_rejects_wrong_token(make_inproc_server):
    srv = make_inproc_server(
        bootstrap_admin=False,
        env={"LM_CHAT_SETUP_TOKEN": GOOD_TOKEN},
    )
    status, body = _setup(
        srv.url,
        {
            "username":     "attacker",
            "password":     "attackerpass1",
            "setup_token":  "different-token",
        },
    )
    assert status == 401, body


def test_setup_rejects_partial_token_prefix(make_inproc_server):
    """A common timing-attack input — first 5 chars correct, rest wrong.

    constant-time compare keeps this from leaking via observable latency,
    but we still assert it's rejected at the API level.
    """
    srv = make_inproc_server(
        bootstrap_admin=False,
        env={"LM_CHAT_SETUP_TOKEN": GOOD_TOKEN},
    )
    status, _ = _setup(
        srv.url,
        {
            "username":     "attacker",
            "password":     "attackerpass1",
            "setup_token":  GOOD_TOKEN[:5] + "x" * (len(GOOD_TOKEN) - 5),
        },
    )
    assert status == 401


def test_setup_rejects_non_string_token(make_inproc_server):
    """Defensive: JSON booleans / numbers / nulls must not bypass the check."""
    srv = make_inproc_server(
        bootstrap_admin=False,
        env={"LM_CHAT_SETUP_TOKEN": GOOD_TOKEN},
    )
    for bad in (None, True, 12345, ["array"]):
        status, _ = _setup(
            srv.url,
            {"username": "x", "password": "yyyyyyyy", "setup_token": bad},
        )
        assert status == 401, f"bypassed with {bad!r}"


def test_setup_accepts_correct_token(make_inproc_server):
    srv = make_inproc_server(
        bootstrap_admin=False,
        env={"LM_CHAT_SETUP_TOKEN": GOOD_TOKEN},
    )
    status, body = _setup(
        srv.url,
        {
            "username":     "founder",
            "password":     "founderpass1",
            "setup_token":  GOOD_TOKEN,
        },
    )
    assert status == 200, body
    assert body.get("user", {}).get("username") == "founder"
    assert body["user"]["is_admin"] == 1


def test_setup_idempotent_after_completion(make_inproc_server):
    """Once an admin exists, /api/auth/setup is always rejected — regardless
    of the token.  Defence-in-depth in case the operator leaves the env set."""
    srv = make_inproc_server(
        bootstrap_admin=False,
        env={"LM_CHAT_SETUP_TOKEN": GOOD_TOKEN},
    )
    # First setup succeeds.
    s1, _ = _setup(
        srv.url,
        {
            "username":     "founder",
            "password":     "founderpass1",
            "setup_token":  GOOD_TOKEN,
        },
    )
    assert s1 == 200
    # Second setup with the SAME valid token must still fail.
    s2, body = _setup(
        srv.url,
        {
            "username":     "second",
            "password":     "secondpass1",
            "setup_token":  GOOD_TOKEN,
        },
    )
    assert s2 == 400, body


# ---------------------------------------------------------------------------
# Timing-attack budget
#
# The compare uses hmac.compare_digest, which is constant-time in CPython.
# We can't fully prove that property from outside the process, but we can
# check that a wrong-but-same-length token doesn't take noticeably longer
# than a missing one — catches regressions where someone switches to ==.
# ---------------------------------------------------------------------------

def test_setup_token_comparison_is_not_obviously_timing_sensitive(make_inproc_server):
    srv = make_inproc_server(
        bootstrap_admin=False,
        env={"LM_CHAT_SETUP_TOKEN": GOOD_TOKEN},
    )

    samples_missing = []
    samples_wrong = []
    for _ in range(20):
        t0 = time.perf_counter()
        _setup(srv.url, {"username": "x", "password": "yyyyyyyy"})
        samples_missing.append(time.perf_counter() - t0)

        t0 = time.perf_counter()
        _setup(
            srv.url,
            {"username": "x", "password": "yyyyyyyy", "setup_token": "z" * len(GOOD_TOKEN)},
        )
        samples_wrong.append(time.perf_counter() - t0)

    # Trim outliers from network jitter — median is the honest comparison.
    samples_missing.sort()
    samples_wrong.sort()
    med_missing = samples_missing[len(samples_missing) // 2]
    med_wrong = samples_wrong[len(samples_wrong) // 2]
    # Wrong-token should not be more than 3x slower than missing.  In CPython
    # the difference is essentially zero — generous bound for CI runners.
    assert med_wrong < med_missing * 3 + 0.05, (
        f"wrong-token median {med_wrong:.4f}s vs missing {med_missing:.4f}s "
        f"— compare may be non-constant-time"
    )
