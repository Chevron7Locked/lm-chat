"""
Tests for the chat share link API.

Routes covered:
  POST   /api/chats/{id}/share    — create share link
  DELETE /api/chats/{id}/share    — remove share link
  GET    /share/{share_id}        — public share page (no auth)
"""

import json, urllib.error, urllib.request

import pytest

from conftest import _Client, _create_chat, ADMIN_USER, ADMIN_PASS, CSRF_HEADER


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_share(client: _Client, chat_id: str) -> dict:
    """Create a share link and return the response dict {share_id, url}."""
    resp = client.post(f"/api/chats/{chat_id}/share", {})
    return json.loads(resp.read())


def _get_public_page(base_url: str, share_id: str) -> tuple[int, bytes]:
    """Fetch the public share page. Returns (status_code, body)."""
    try:
        resp = urllib.request.urlopen(f"{base_url}/share/{share_id}", timeout=10)
        return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


# ---------------------------------------------------------------------------
# Share lifecycle
# ---------------------------------------------------------------------------

class TestShareLifecycle:
    def test_create_share_returns_200_and_share_id(self, client):
        chat_id = _create_chat(client)
        resp = client.post(f"/api/chats/{chat_id}/share", {})
        assert resp.status == 200
        data = json.loads(resp.read())
        assert "share_id" in data
        assert data["share_id"]

    def test_create_share_id_is_nonempty_string(self, client):
        chat_id = _create_chat(client)
        data = _create_share(client, chat_id)
        assert isinstance(data["share_id"], str)
        assert len(data["share_id"]) > 0

    def test_delete_share_returns_200(self, client):
        chat_id = _create_chat(client)
        _create_share(client, chat_id)
        resp = client.delete(f"/api/chats/{chat_id}/share")
        assert resp.status == 200

    def test_delete_nonexistent_share_returns_200(self, client):
        # Server does a DELETE with no rows affected and still returns 200 (idempotent).
        chat_id = _create_chat(client)
        resp = client.delete(f"/api/chats/{chat_id}/share")
        assert resp.status == 200

    def test_create_share_on_nonexistent_chat_returns_404(self, authed_client):
        # With auth enabled, _verify_chat_owner queries the DB and returns 404
        # for a chat that does not belong to the user (or doesn't exist at all).
        with pytest.raises(urllib.error.HTTPError) as exc:
            authed_client.admin.post("/api/chats/nonexistent-chat-id-xyz/share", {})
        assert exc.value.code == 404

    def test_create_share_idempotent(self, client):
        chat_id = _create_chat(client)
        data1 = _create_share(client, chat_id)
        data2 = _create_share(client, chat_id)
        assert data1["share_id"] == data2["share_id"]


# ---------------------------------------------------------------------------
# Public share page
# ---------------------------------------------------------------------------

class TestPublicSharePage:
    def test_public_share_accessible_without_auth(self, client, app_server):
        chat_id = _create_chat(client)
        data = _create_share(client, chat_id)
        status, _ = _get_public_page(app_server, data["share_id"])
        assert status == 200

    def test_public_share_returns_html(self, client, app_server):
        chat_id = _create_chat(client)
        data = _create_share(client, chat_id)
        _, body = _get_public_page(app_server, data["share_id"])
        assert b"<html" in body.lower() or b"<!doctype" in body.lower()

    def test_public_share_contains_message_content(self, client, app_server, mock_lmstudio):
        import time
        mock_lmstudio.configure(chunks=["Unique-content-42"])
        chat_id = _create_chat(client)
        # Seed a message into the chat via streaming
        data = json.dumps({
            "model": "test-model", "input": "Hello", "chat_id": chat_id
        }).encode()
        r = urllib.request.Request(
            app_server + "/api/chat/stream", data=data,
            headers={"Content-Type": "application/json", "X-Requested-With": "lm-chat"},
            method="POST",
        )
        if client.cookie:
            r.add_header("Cookie", client.cookie)
        urllib.request.urlopen(r, timeout=15).read()
        time.sleep(0.2)
        share_data = _create_share(client, chat_id)
        _, body = _get_public_page(app_server, share_data["share_id"])
        assert b"Unique-content-42" in body

    def test_invalid_share_id_returns_404(self, client, app_server):
        status, _ = _get_public_page(app_server, "invalid-share-id-xyz-never-exists")
        assert status == 404

    def test_public_share_after_delete_returns_404(self, client, app_server):
        chat_id = _create_chat(client)
        share_data = _create_share(client, chat_id)
        client.delete(f"/api/chats/{chat_id}/share")
        status, _ = _get_public_page(app_server, share_data["share_id"])
        assert status == 404


# ---------------------------------------------------------------------------
# Share isolation
# ---------------------------------------------------------------------------

class TestShareIsolation:
    def test_user_cannot_delete_other_users_share(self, authed_client):
        chat_id = _create_chat(authed_client.admin)
        _create_share(authed_client.admin, chat_id)
        with pytest.raises(urllib.error.HTTPError) as exc:
            authed_client.user.delete(f"/api/chats/{chat_id}/share")
        assert exc.value.code in (403, 404)

    def test_user_cannot_share_other_users_chat(self, authed_client):
        chat_id = _create_chat(authed_client.admin)
        with pytest.raises(urllib.error.HTTPError) as exc:
            authed_client.user.post(f"/api/chats/{chat_id}/share", {})
        assert exc.value.code in (403, 404)

    def test_unauthenticated_cannot_create_share(self, app_server_auth, authed_client):
        chat_id = _create_chat(authed_client.admin)
        anon = authed_client.anon()
        with pytest.raises(urllib.error.HTTPError) as exc:
            anon.post(f"/api/chats/{chat_id}/share", {})
        assert exc.value.code == 401

    def test_unauthenticated_cannot_delete_share(self, app_server_auth, authed_client):
        chat_id = _create_chat(authed_client.admin)
        _create_share(authed_client.admin, chat_id)
        anon = authed_client.anon()
        with pytest.raises(urllib.error.HTTPError) as exc:
            anon.delete(f"/api/chats/{chat_id}/share")
        assert exc.value.code == 401

    def test_share_deleted_when_chat_deleted(self, client, app_server):
        chat_id = _create_chat(client)
        share_data = _create_share(client, chat_id)
        client.delete(f"/api/chats/{chat_id}")
        status, _ = _get_public_page(app_server, share_data["share_id"])
        assert status == 404
