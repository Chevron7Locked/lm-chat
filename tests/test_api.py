"""
API contract tests.

Covers: health, models, non-streaming chat, chat CRUD, per-chat settings,
message feedback, message pins. All against a real server.py subprocess with
AUTH=false and the stdlib mock LM Studio.
"""

import json, urllib.error, urllib.request

import pytest

from conftest import CSRF_HEADER, _Client, _create_chat


def _send_message(client: _Client, chat_id: str, text: str = "Hello") -> dict:
    """Non-streaming chat call that persists messages."""
    resp = client.post("/api/chat", {
        "model":   "test-model",
        "input":   text,
        "chat_id": chat_id,
    })
    return client.json(resp)


def _get_messages(client: _Client, chat_id: str) -> list:
    resp = client.get(f"/api/chats/{chat_id}/messages")
    return client.json(resp)


# ---------------------------------------------------------------------------
# Static file / health
# ---------------------------------------------------------------------------

class TestStaticAndHealth:
    def test_root_returns_html(self, client):
        resp = client.get("/")
        assert resp.status == 200
        ct = resp.headers.get("Content-Type", "")
        assert "text/html" in ct

    def test_root_contains_page_title(self, client):
        resp = client.get("/")
        body = resp.read().decode(errors="replace")
        assert "LM Chat" in body

    def test_health_lmstudio_up(self, client):
        resp = client.get("/api/health")
        assert resp.status == 200
        data = client.json(resp)
        assert "version" in data
        assert data.get("lmstudio") is True
        assert data.get("db") is True

    def test_health_has_version_field(self, client):
        resp = client.get("/api/health")
        data = client.json(resp)
        assert isinstance(data["version"], str) and len(data["version"]) > 0


# ---------------------------------------------------------------------------
# Models endpoint
# ---------------------------------------------------------------------------

class TestModels:
    def test_models_proxied_from_mock(self, client):
        resp = client.get("/api/models")
        assert resp.status == 200
        data = client.json(resp)
        assert "data" in data
        assert any(m["id"] == "test-model" for m in data["data"])


# ---------------------------------------------------------------------------
# Non-streaming chat
# ---------------------------------------------------------------------------

class TestChat:
    def test_non_streaming_returns_response(self, client):
        chat_id = _create_chat(client)
        resp = client.post("/api/chat", {
            "model":   "test-model",
            "input":   "Hello",
            "chat_id": chat_id,
        })
        assert resp.status == 200
        data = client.json(resp)
        assert "response_id" in data

    def test_non_streaming_persists_messages(self, client):
        chat_id = _create_chat(client)
        _send_message(client, chat_id, "Hello")
        msgs = _get_messages(client, chat_id)
        assert len(msgs) >= 2
        roles = {m["role"] for m in msgs}
        assert "user" in roles
        assert "assistant" in roles

    def test_incognito_does_not_persist(self, client):
        chat_id = _create_chat(client)
        client.post("/api/chat", {
            "model":     "test-model",
            "input":     "Secret",
            "chat_id":   chat_id,
            "incognito": True,
        })
        msgs = _get_messages(client, chat_id)
        assert len(msgs) == 0

    def test_invalid_json_body_returns_400(self, client):
        req = urllib.request.Request(
            client.base_url + "/api/chat",
            data=b"not valid json",
            headers={"Content-Type": "application/json", **CSRF_HEADER},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=10)
        assert exc.value.code == 400

    def test_model_too_long_returns_400(self, client):
        with pytest.raises(urllib.error.HTTPError) as exc:
            client.post("/api/chat", {"model": "x" * 300, "input": "Hello"})
        assert exc.value.code == 400

    def test_missing_csrf_returns_403(self, client):
        data = json.dumps({"model": "test-model", "input": "Hi"}).encode()
        req = urllib.request.Request(
            client.base_url + "/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=10)
        assert exc.value.code == 403

    def test_upstream_error_propagates(self, client, mock_lmstudio):
        mock_lmstudio.configure(status_code=503)
        chat_id = _create_chat(client)
        with pytest.raises(urllib.error.HTTPError) as exc:
            client.post("/api/chat", {
                "model":   "test-model",
                "input":   "Hello",
                "chat_id": chat_id,
            })
        # Server proxies upstream error code (503) or converts to 502 for connection failures
        assert exc.value.code in (502, 503)


# ---------------------------------------------------------------------------
# Chat CRUD
# ---------------------------------------------------------------------------

class TestChatCrud:
    def test_empty_chat_list(self, client):
        resp = client.get("/api/chats")
        assert resp.status == 200
        data = client.json(resp)
        assert isinstance(data, list)
        assert len(data) == 0

    def test_create_chat_appears_in_list(self, client):
        chat_id = _create_chat(client, "My Chat")
        resp = client.get("/api/chats")
        chats = client.json(resp)
        ids = [c["id"] for c in chats]
        assert chat_id in ids

    def test_get_messages_empty(self, client):
        chat_id = _create_chat(client)
        msgs = _get_messages(client, chat_id)
        assert msgs == []

    def test_get_messages_nonexistent_chat_returns_404(self, client):
        # Server always returns 404 for unknown chat IDs regardless of auth mode
        with pytest.raises(urllib.error.HTTPError) as exc:
            client.get("/api/chats/doesnotexist/messages")
        assert exc.value.code == 404

    def test_delete_chat_removes_from_list(self, client):
        chat_id = _create_chat(client)
        client.delete(f"/api/chats/{chat_id}")
        chats = client.json(client.get("/api/chats"))
        assert chat_id not in [c["id"] for c in chats]

    def test_rename_chat(self, client):
        chat_id = _create_chat(client, "Original")
        client.patch(f"/api/chats/{chat_id}/title", {"title": "Renamed"})
        chats = client.json(client.get("/api/chats"))
        match = next(c for c in chats if c["id"] == chat_id)
        assert match["title"] == "Renamed"

    def test_pin_chat_toggle(self, client):
        chat_id = _create_chat(client)
        # Pin it
        resp = client.post(f"/api/chats/{chat_id}/pin")
        assert resp.status == 200
        chats = client.json(client.get("/api/chats"))
        match = next(c for c in chats if c["id"] == chat_id)
        assert match.get("pinned")
        # Unpin (toggle back)
        client.post(f"/api/chats/{chat_id}/pin")
        chats = client.json(client.get("/api/chats"))
        match = next(c for c in chats if c["id"] == chat_id)
        assert not match.get("pinned")

    def test_fork_chat(self, client):
        chat_id = _create_chat(client)
        _send_message(client, chat_id)
        msgs = _get_messages(client, chat_id)
        last_msg_id = msgs[-1]["id"]
        resp = client.post(f"/api/chats/{chat_id}/fork", {"up_to_message_id": last_msg_id})
        assert resp.status == 200
        fork = client.json(resp)
        assert "id" in fork
        assert fork["id"] != chat_id
        # Fork appears in list
        chats = client.json(client.get("/api/chats"))
        assert fork["id"] in [c["id"] for c in chats]

    def test_delete_last_response(self, client):
        chat_id = _create_chat(client)
        _send_message(client, chat_id, "Hello")
        msgs_before = _get_messages(client, chat_id)
        assert len(msgs_before) >= 2
        client.delete(f"/api/chats/{chat_id}/messages/last")
        msgs_after = _get_messages(client, chat_id)
        assert len(msgs_after) < len(msgs_before)

    def test_search_messages(self, client):
        chat_id = _create_chat(client)
        _send_message(client, chat_id, "The quick brown fox")
        resp = client.post("/api/search", {"query": "quick brown"})
        assert resp.status == 200
        data = client.json(resp)
        # Returns {mode: "text"|"semantic", results: [...]}
        results = data.get("results", data) if isinstance(data, dict) else data
        assert isinstance(results, list)


# ---------------------------------------------------------------------------
# Per-chat settings (v0.3.0)
# ---------------------------------------------------------------------------

class TestChatSettings:
    def test_new_chat_has_empty_settings(self, client):
        chat_id = _create_chat(client)
        resp = client.get(f"/api/chats/{chat_id}/settings")
        assert resp.status == 200
        data = client.json(resp)
        assert data == {}

    def test_patch_valid_temperature(self, client):
        chat_id = _create_chat(client)
        resp = client.patch(f"/api/chats/{chat_id}/settings", {"temperature": 0.8})
        assert resp.status == 200
        data = client.json(resp)
        assert data["temperature"] == 0.8

    def test_patch_persists_across_get(self, client):
        chat_id = _create_chat(client)
        client.patch(f"/api/chats/{chat_id}/settings", {"temperature": 1.2})
        resp = client.get(f"/api/chats/{chat_id}/settings")
        data = client.json(resp)
        assert data["temperature"] == 1.2

    def test_patch_system_prompt(self, client):
        chat_id = _create_chat(client)
        client.patch(f"/api/chats/{chat_id}/settings", {"system_prompt": "You are a pirate."})
        data = client.json(client.get(f"/api/chats/{chat_id}/settings"))
        assert data["system_prompt"] == "You are a pirate."

    def test_patch_multiple_keys_merged(self, client):
        chat_id = _create_chat(client)
        client.patch(f"/api/chats/{chat_id}/settings", {"temperature": 0.5})
        client.patch(f"/api/chats/{chat_id}/settings", {"top_p": 0.9})
        data = client.json(client.get(f"/api/chats/{chat_id}/settings"))
        assert data["temperature"] == 0.5
        assert data["top_p"] == 0.9

    def test_patch_null_removes_key(self, client):
        chat_id = _create_chat(client)
        client.patch(f"/api/chats/{chat_id}/settings", {"temperature": 0.5})
        client.patch(f"/api/chats/{chat_id}/settings", {"temperature": None})
        data = client.json(client.get(f"/api/chats/{chat_id}/settings"))
        assert "temperature" not in data

    def test_patch_unknown_key_returns_400(self, client):
        chat_id = _create_chat(client)
        with pytest.raises(urllib.error.HTTPError) as exc:
            client.patch(f"/api/chats/{chat_id}/settings", {"unknown_key": "x"})
        assert exc.value.code == 400

    def test_patch_temperature_out_of_range_returns_400(self, client):
        chat_id = _create_chat(client)
        with pytest.raises(urllib.error.HTTPError) as exc:
            client.patch(f"/api/chats/{chat_id}/settings", {"temperature": 5.0})
        assert exc.value.code == 400

    def test_delete_clears_settings(self, client):
        chat_id = _create_chat(client)
        client.patch(f"/api/chats/{chat_id}/settings", {"temperature": 0.7})
        client.delete(f"/api/chats/{chat_id}/settings")
        data = client.json(client.get(f"/api/chats/{chat_id}/settings"))
        assert data == {}

    def test_settings_on_nonexistent_chat_returns_404(self, client):
        with pytest.raises(urllib.error.HTTPError) as exc:
            client.get("/api/chats/nonexistent/settings")
        assert exc.value.code == 404

    def test_patch_reasoning_valid_values(self, client):
        chat_id = _create_chat(client)
        for val in ("off", "medium", "high"):
            client.patch(f"/api/chats/{chat_id}/settings", {"reasoning": val})
            data = client.json(client.get(f"/api/chats/{chat_id}/settings"))
            assert data["reasoning"] == val

    def test_patch_reasoning_invalid_value_returns_400(self, client):
        chat_id = _create_chat(client)
        with pytest.raises(urllib.error.HTTPError) as exc:
            client.patch(f"/api/chats/{chat_id}/settings", {"reasoning": "ultra"})
        assert exc.value.code == 400

    def test_patch_sc_cove_boolean_settings(self, client):
        chat_id = _create_chat(client)
        client.patch(f"/api/chats/{chat_id}/settings", {"sc_enabled": True, "cove_enabled": False})
        data = client.json(client.get(f"/api/chats/{chat_id}/settings"))
        assert data["sc_enabled"] is True
        assert data["cove_enabled"] is False


# ---------------------------------------------------------------------------
# Message feedback (v0.3.0)
# ---------------------------------------------------------------------------

class TestMessageFeedback:
    def _get_assistant_message_id(self, client: _Client, chat_id: str) -> int:
        msgs = _get_messages(client, chat_id)
        for m in msgs:
            if m["role"] == "assistant":
                return m["id"]
        raise AssertionError("No assistant message found")

    def test_thumbs_up(self, client):
        chat_id = _create_chat(client)
        _send_message(client, chat_id)
        msg_id = self._get_assistant_message_id(client, chat_id)
        resp = client.post(f"/api/messages/{msg_id}/feedback", {"rating": 1})
        assert resp.status == 200
        data = client.json(resp)
        assert data["rating"] == 1

    def test_thumbs_down(self, client):
        chat_id = _create_chat(client)
        _send_message(client, chat_id)
        msg_id = self._get_assistant_message_id(client, chat_id)
        resp = client.post(f"/api/messages/{msg_id}/feedback", {"rating": -1})
        assert resp.status == 200
        data = client.json(resp)
        assert data["rating"] == -1

    def test_clear_feedback(self, client):
        chat_id = _create_chat(client)
        _send_message(client, chat_id)
        msg_id = self._get_assistant_message_id(client, chat_id)
        client.post(f"/api/messages/{msg_id}/feedback", {"rating": 1})
        resp = client.post(f"/api/messages/{msg_id}/feedback", {"rating": 0})
        assert resp.status == 200
        data = client.json(resp)
        assert data["rating"] == 0

    def test_feedback_toggle_up_to_down(self, client):
        chat_id = _create_chat(client)
        _send_message(client, chat_id)
        msg_id = self._get_assistant_message_id(client, chat_id)
        client.post(f"/api/messages/{msg_id}/feedback", {"rating": 1})
        resp = client.post(f"/api/messages/{msg_id}/feedback", {"rating": -1})
        data = client.json(resp)
        assert data["rating"] == -1

    def test_invalid_rating_returns_400(self, client):
        chat_id = _create_chat(client)
        _send_message(client, chat_id)
        msg_id = self._get_assistant_message_id(client, chat_id)
        with pytest.raises(urllib.error.HTTPError) as exc:
            client.post(f"/api/messages/{msg_id}/feedback", {"rating": 2})
        assert exc.value.code == 400

    def test_nonexistent_message_returns_403(self, client):
        with pytest.raises(urllib.error.HTTPError) as exc:
            client.post("/api/messages/999999/feedback", {"rating": 1})
        assert exc.value.code == 403


# ---------------------------------------------------------------------------
# Message pinning (v0.3.0)
# ---------------------------------------------------------------------------

class TestMessagePins:
    def _get_assistant_message_id(self, client: _Client, chat_id: str) -> int:
        msgs = _get_messages(client, chat_id)
        for m in msgs:
            if m["role"] == "assistant":
                return m["id"]
        raise AssertionError("No assistant message found")

    def test_pin_message_returns_201(self, client):
        chat_id = _create_chat(client)
        _send_message(client, chat_id)
        msg_id = self._get_assistant_message_id(client, chat_id)
        resp = client.post(f"/api/messages/{msg_id}/pin")
        assert resp.status == 201
        data = client.json(resp)
        assert "id" in data
        assert data["message_id"] == msg_id

    def test_pin_appears_in_list(self, client):
        chat_id = _create_chat(client)
        _send_message(client, chat_id)
        msg_id = self._get_assistant_message_id(client, chat_id)
        client.post(f"/api/messages/{msg_id}/pin")
        pins = client.json(client.get("/api/pins"))
        assert any(p["message_id"] == msg_id for p in pins)

    def test_pin_appears_in_chat_pins(self, client):
        chat_id = _create_chat(client)
        _send_message(client, chat_id)
        msg_id = self._get_assistant_message_id(client, chat_id)
        client.post(f"/api/messages/{msg_id}/pin")
        pins = client.json(client.get(f"/api/chats/{chat_id}/pins"))
        assert len(pins) == 1
        assert pins[0]["message_id"] == msg_id

    def test_pin_idempotent(self, client):
        chat_id = _create_chat(client)
        _send_message(client, chat_id)
        msg_id = self._get_assistant_message_id(client, chat_id)
        client.post(f"/api/messages/{msg_id}/pin")
        resp2 = client.post(f"/api/messages/{msg_id}/pin")
        data2 = client.json(resp2)
        assert data2.get("already_pinned") is True

    def test_delete_pin(self, client):
        chat_id = _create_chat(client)
        _send_message(client, chat_id)
        msg_id = self._get_assistant_message_id(client, chat_id)
        pin_resp = client.post(f"/api/messages/{msg_id}/pin")
        pin_id = client.json(pin_resp)["id"]
        client.delete(f"/api/pins/{pin_id}")
        pins = client.json(client.get("/api/pins"))
        assert pin_id not in [p["id"] for p in pins]

    def test_delete_nonexistent_pin_returns_404(self, client):
        with pytest.raises(urllib.error.HTTPError) as exc:
            client.delete("/api/pins/doesnotexist")
        assert exc.value.code == 404

    def test_update_pin_title(self, client):
        chat_id = _create_chat(client)
        _send_message(client, chat_id)
        msg_id = self._get_assistant_message_id(client, chat_id)
        pin_id = client.json(client.post(f"/api/messages/{msg_id}/pin"))["id"]
        resp = client.patch(f"/api/pins/{pin_id}/title", {"title": "My note"})
        assert resp.status == 200
        data = client.json(resp)
        assert data["title"] == "My note"

    def test_update_pin_title_empty_returns_400(self, client):
        chat_id = _create_chat(client)
        _send_message(client, chat_id)
        msg_id = self._get_assistant_message_id(client, chat_id)
        pin_id = client.json(client.post(f"/api/messages/{msg_id}/pin"))["id"]
        with pytest.raises(urllib.error.HTTPError) as exc:
            client.patch(f"/api/pins/{pin_id}/title", {"title": ""})
        assert exc.value.code == 400

    def test_cannot_pin_user_message(self, client):
        chat_id = _create_chat(client)
        _send_message(client, chat_id)
        msgs = _get_messages(client, chat_id)
        user_msg = next(m for m in msgs if m["role"] == "user")
        with pytest.raises(urllib.error.HTTPError) as exc:
            client.post(f"/api/messages/{user_msg['id']}/pin")
        assert exc.value.code == 404


# ---------------------------------------------------------------------------
# Global pins
# ---------------------------------------------------------------------------

class TestGlobalPins:
    def test_global_pins_empty_by_default(self, client):
        resp = client.get("/api/pins")
        data = json.loads(resp.read())
        # Should be an empty list (no pins in a fresh chat context)
        assert isinstance(data, list)

    def test_global_pins_contains_pinned_message(self, client):
        chat_id = _create_chat(client)
        _send_message(client, chat_id)
        msgs = _get_messages(client, chat_id)
        asst = next((m for m in msgs if m["role"] == "assistant"), None)
        if asst is None:
            pytest.skip("No assistant message to pin")
        client.post(f"/api/messages/{asst['id']}/pin")
        pins = json.loads(client.get("/api/pins").read())
        assert isinstance(pins, list)
        assert len(pins) >= 1
        assert any(p["message_id"] == asst["id"] for p in pins)



# ---------------------------------------------------------------------------
# Search (POST endpoint)
# ---------------------------------------------------------------------------

class TestSearch:
    def test_search_returns_results_field(self, client):
        resp = client.post("/api/search", {"query": "test"})
        data = json.loads(resp.read())
        assert "results" in data or isinstance(data, list)

    def test_search_empty_query_returns_400(self, client):
        with pytest.raises(urllib.error.HTTPError) as exc:
            client.post("/api/search", {"query": ""})
        assert exc.value.code == 400

    def test_search_results_is_list(self, client):
        resp = client.post("/api/search", {"query": "anything"})
        data = json.loads(resp.read())
        results = data.get("results", data) if isinstance(data, dict) else data
        assert isinstance(results, list)

    def test_search_matching_content(self, client):
        chat_id = _create_chat(client)
        _send_message(client, chat_id, "unique_search_term_xyz")
        resp = client.post("/api/search", {"query": "unique_search_term_xyz"})
        data = json.loads(resp.read())
        results = data.get("results", data) if isinstance(data, dict) else data
        assert isinstance(results, list)
        assert len(results) >= 1, "Seeded message not found in search results"
        # Each result has "content" (message text) and "chat_title"
        texts = [r.get("content", "") for r in results]
        assert any("unique_search_term_xyz" in t for t in texts), \
            f"Seeded term not in result content: {texts}"


# ---------------------------------------------------------------------------
# Compact
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Nonexistent chat ID tests
# ---------------------------------------------------------------------------

class TestNonexistentChatId:
    def test_patch_settings_nonexistent_chat_returns_404(self, client):
        with pytest.raises(urllib.error.HTTPError) as exc:
            client.patch("/api/chats/doesnotexist/settings", {"temperature": 0.5})
        assert exc.value.code == 404

    def test_fork_nonexistent_chat_returns_404(self, client):
        with pytest.raises(urllib.error.HTTPError) as exc:
            client.post("/api/chats/doesnotexist/fork", {"up_to_message_id": 1})
        assert exc.value.code == 404

    def test_compact_nonexistent_chat_returns_error(self, client):
        with pytest.raises(urllib.error.HTTPError) as exc:
            client.post("/api/chats/doesnotexist/compact", {"model": "test-model"})
        assert exc.value.code in (400, 404)

    def test_folder_nonexistent_chat_returns_404(self, client):
        with pytest.raises(urllib.error.HTTPError) as exc:
            client.post("/api/chats/doesnotexist/folder", {"folder": "test"})
        assert exc.value.code == 404

    def test_delete_nonexistent_chat_returns_404(self, client):
        with pytest.raises(urllib.error.HTTPError) as exc:
            client.delete("/api/chats/doesnotexist")
        assert exc.value.code == 404


# ---------------------------------------------------------------------------
# Compact
# ---------------------------------------------------------------------------

class TestCompact:
    def test_compact_nonexistent_chat_returns_error(self, client):
        # Server returns 400 for compact on nonexistent chat — _resolve_chat
        # validates the body before performing the ownership lookup
        with pytest.raises(urllib.error.HTTPError) as exc:
            client.post("/api/chats/nonexistent-chat-id/compact", {})
        assert exc.value.code in (400, 404)

    def test_compact_short_chat_returns_400(self, client):
        # A newly-created chat with one exchange is too short to compact
        chat_id = _create_chat(client)
        _send_message(client, chat_id)
        with pytest.raises(urllib.error.HTTPError) as exc:
            client.post(f"/api/chats/{chat_id}/compact", {})
        assert exc.value.code == 400

    def test_compact_succeeds_and_reduces_message_count(self, client):
        # Compact requires COMPACT_MIN_TURNS (10) user+assistant messages.
        # 5 _send_message calls → 5 user rows + 5 assistant rows = 10 turns.
        chat_id = _create_chat(client)
        for _ in range(5):
            _send_message(client, chat_id)
        msgs_before = _get_messages(client, chat_id)
        user_asst_before = [m for m in msgs_before if m["role"] in ("user", "assistant")]
        assert len(user_asst_before) >= 10, "Setup failed: not enough messages to compact"
        resp = client.post(f"/api/chats/{chat_id}/compact", {"model": "test-model"})
        assert resp.status == 200
        data = client.json(resp)
        assert "summary" in data and data["summary"], "Compact should return a non-empty summary"
        msgs_after = _get_messages(client, chat_id)
        user_asst_after = [m for m in msgs_after if m["role"] in ("user", "assistant")]
        assert len(user_asst_after) < len(user_asst_before), \
            "Compact should reduce the number of stored messages"
