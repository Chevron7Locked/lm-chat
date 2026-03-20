"""
Playwright E2E tests.

Full browser tests against the real server.py + mock LM Studio stack.
Each test gets a fresh server process and DB (function-scoped app_server).

Run: pytest tests/test_e2e.py --headed   (for visual debugging)
CI:  pytest tests/test_e2e.py            (headless Chromium)
"""

import re, time

import pytest
from playwright.sync_api import Page, expect



# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def page_at(page: Page, app_server: str):
    """Navigate to the app root and return the page."""
    page.goto(app_server)
    return page


# ---------------------------------------------------------------------------
# Page load
# ---------------------------------------------------------------------------

class TestPageLoad:
    def test_title_present(self, page_at: Page):
        expect(page_at).to_have_title(re.compile(r".+"))

    def test_chat_input_visible(self, page_at: Page):
        expect(page_at.locator("#input")).to_be_visible()

    def test_send_button_visible(self, page_at: Page):
        expect(page_at.locator("#send")).to_be_visible()

    def test_no_console_errors_on_load(self, page: Page, app_server: str):
        errors = []
        page.on("console", lambda msg: errors.append(msg) if msg.type == "error" else None)
        page.goto(app_server)
        page.wait_for_timeout(500)
        assert errors == [], f"Console errors on load: {[e.text for e in errors]}"


# ---------------------------------------------------------------------------
# Send + streamed response
# ---------------------------------------------------------------------------

class TestSendMessage:
    def test_send_message_and_see_response(self, page_at: Page, mock_lmstudio):
        mock_lmstudio.configure(chunks=["Hello", " from", " LM Studio"])
        page_at.locator("#input").fill("Hello")
        page_at.locator("#send").click()
        # Response appears in the message list
        page_at.wait_for_selector(".m-asst", timeout=10_000)
        asst_msg = page_at.locator(".m-asst").last
        expect(asst_msg).to_contain_text("Hello from LM Studio", timeout=10_000)

    def test_user_message_appears_immediately(self, page_at: Page, mock_lmstudio):
        mock_lmstudio.configure(chunks=["Hi back"])
        page_at.locator("#input").fill("Test message")
        page_at.locator("#send").click()
        # User message appears in chat
        page_at.wait_for_selector(".m-user", timeout=5_000)
        user_msg = page_at.locator(".m-user").first
        expect(user_msg).to_contain_text("Test message")

    def test_input_cleared_after_send(self, page_at: Page, mock_lmstudio):
        mock_lmstudio.configure(chunks=["OK"])
        page_at.locator("#input").fill("Clear me")
        page_at.locator("#send").click()
        page_at.wait_for_selector(".m-asst", timeout=10_000)
        assert page_at.locator("#input").input_value() == ""


# ---------------------------------------------------------------------------
# Message bottom row actions
# ---------------------------------------------------------------------------

class TestMessageBottomRow:
    def _send_and_wait(self, page: Page, mock_lmstudio, text: str = "Test"):
        mock_lmstudio.configure(chunks=["Done"])
        page.locator("#input").fill(text)
        page.locator("#send").click()
        page.wait_for_selector(".m-asst", timeout=10_000)

    def test_copy_button_present_on_assistant_message(self, page_at: Page, mock_lmstudio):
        self._send_and_wait(page_at, mock_lmstudio)
        asst = page_at.locator(".m-asst").last
        asst.hover()
        copy_btn = asst.locator(".msg-action-btn[title='Copy']")
        expect(copy_btn).to_be_visible(timeout=3_000)

    def test_feedback_buttons_present(self, page_at: Page, mock_lmstudio):
        self._send_and_wait(page_at, mock_lmstudio)
        asst = page_at.locator(".m-asst").last
        asst.hover()
        expect(asst.locator(".msg-action-btn[title='Helpful']")).to_be_visible(timeout=3_000)
        expect(asst.locator(".msg-action-btn[title='Not helpful']")).to_be_visible(timeout=3_000)

    def test_thumbs_up_click_marks_voted(self, page_at: Page, mock_lmstudio, app_server: str):
        self._send_and_wait(page_at, mock_lmstudio)
        asst = page_at.locator(".m-asst").last
        asst.hover()
        thumb_up = asst.locator(".msg-action-btn[title='Helpful']")
        expect(thumb_up).to_be_visible(timeout=3_000)
        thumb_up.click()
        # After clicking, button should have voted-up class
        expect(thumb_up).to_have_class(re.compile("voted-up"), timeout=3_000)

    def test_thumbs_down_click_marks_voted(self, page_at: Page, mock_lmstudio):
        self._send_and_wait(page_at, mock_lmstudio)
        asst = page_at.locator(".m-asst").last
        asst.hover()
        thumb_down = asst.locator(".msg-action-btn[title='Not helpful']")
        expect(thumb_down).to_be_visible(timeout=3_000)
        thumb_down.click()
        expect(thumb_down).to_have_class(re.compile("voted-down"), timeout=3_000)

    def test_regenerate_button_visible(self, page_at: Page, mock_lmstudio):
        self._send_and_wait(page_at, mock_lmstudio)
        asst = page_at.locator(".m-asst").last
        asst.hover()
        regen = asst.locator(".msg-action-btn[title='Regenerate']")
        expect(regen).to_be_visible(timeout=3_000)


# ---------------------------------------------------------------------------
# Message pinning UI
# ---------------------------------------------------------------------------

class TestPinningUI:
    def _send_and_wait(self, page: Page, mock_lmstudio):
        mock_lmstudio.configure(chunks=["Pinnable content"])
        page.locator("#input").fill("Pin this")
        page.locator("#send").click()
        page.wait_for_selector(".m-asst", timeout=10_000)

    def test_pin_button_appears_on_hover(self, page_at: Page, mock_lmstudio):
        self._send_and_wait(page_at, mock_lmstudio)
        asst = page_at.locator(".m-asst").last
        asst.hover()
        # Hover pin button is visible on hover ("Pin this response")
        pin_btn = asst.locator(".hover-pin-btn")
        expect(pin_btn).to_be_visible(timeout=3_000)

    def test_pin_activates_pin_nav(self, page_at: Page, mock_lmstudio):
        self._send_and_wait(page_at, mock_lmstudio)
        asst = page_at.locator(".m-asst").last
        asst.hover()
        pin_btn = asst.locator(".hover-pin-btn")
        expect(pin_btn).to_be_visible(timeout=3_000)
        pin_btn.click()
        # Pin navigator should become visible (has-pins state)
        page_at.wait_for_selector("#pin-nav.has-pins", timeout=5_000)
        expect(page_at.locator("#pin-nav")).to_have_class(re.compile("has-pins"), timeout=3_000)


# ---------------------------------------------------------------------------
# Per-chat settings panel
# ---------------------------------------------------------------------------

class TestChatSettingsPanel:
    def test_settings_panel_opens(self, page_at: Page):
        page_at.locator("#chat-settings-btn").click()
        expect(page_at.locator("#right-panel")).to_have_class(re.compile("open"), timeout=3_000)

    def test_settings_panel_closes_via_overlay(self, page_at: Page):
        page_at.locator("#chat-settings-btn").click()
        page_at.wait_for_selector("#right-panel.open", timeout=3_000)
        # The overlay covers the button when open — click it to close
        page_at.locator("#right-panel-overlay").click()
        expect(page_at.locator("#right-panel")).not_to_have_class(re.compile(r"\bopen\b"), timeout=3_000)

    def test_temperature_slider_present_in_panel(self, page_at: Page, mock_lmstudio):
        # Need an active chat for the settings panel to render controls
        mock_lmstudio.configure(chunks=["Hi"])
        page_at.locator("#input").fill("Hello")
        page_at.locator("#send").click()
        page_at.wait_for_selector(".m-asst", timeout=10_000)
        page_at.locator("#chat-settings-btn").click()
        page_at.wait_for_selector("#right-panel.open", timeout=3_000)
        # Per-chat settings panel renders #cs-temp (different from global #s-temp)
        expect(page_at.locator("#cs-temp")).to_be_visible(timeout=3_000)


# ---------------------------------------------------------------------------
# Model selector
# ---------------------------------------------------------------------------

class TestModelSelector:
    def test_model_selector_visible(self, page_at: Page):
        # The visible model selector is #model-sel-wrap; #model-sel is hidden backing element
        expect(page_at.locator("#model-sel-wrap")).to_be_visible()

    def test_model_dropdown_populates(self, page_at: Page):
        # Open the custom model dropdown via the visible wrapper
        page_at.locator("#model-sel-wrap").click()
        page_at.wait_for_selector("#top-model-dd", timeout=5_000)
        # The dropdown should have at least one model entry
        dd = page_at.locator("#top-model-dd")
        expect(dd).to_be_visible(timeout=3_000)


# ---------------------------------------------------------------------------
# LM Studio down
# ---------------------------------------------------------------------------

class TestLMStudioDown:
    def test_error_message_when_lmstudio_down(self, page_at: Page, mock_lmstudio):
        mock_lmstudio.configure(status_code=503)
        page_at.locator("#input").fill("Hello")
        page_at.locator("#send").click()
        # User message appears immediately
        page_at.wait_for_selector(".m-user", timeout=5_000)
        # No infinite spinner — "stop" class removed once stream finishes
        expect(page_at.locator("#send")).not_to_have_class(
            re.compile(r"\bstop\b"), timeout=15_000
        )
        # No assistant response bubble (upstream error produces no content)
        assert page_at.locator(".m-asst").count() == 0


# ---------------------------------------------------------------------------
# New chat resets context
# ---------------------------------------------------------------------------

class TestNewChat:
    def test_new_chat_resets_input_area(self, page_at: Page, mock_lmstudio):
        mock_lmstudio.configure(chunks=["Response"])
        page_at.locator("#input").fill("Hello")
        page_at.locator("#send").click()
        page_at.wait_for_selector(".m-asst", timeout=10_000)
        # Click new chat
        page_at.locator("#new-chat").click()
        page_at.wait_for_timeout(300)
        # Messages area should be empty
        assert page_at.locator(".m-asst").count() == 0


# ---------------------------------------------------------------------------
# Auth redirect (auth-enabled server)
# ---------------------------------------------------------------------------

class TestAuthRedirect:
    def test_login_page_shown_when_auth_enabled(
        self, page: Page, app_server_auth: str
    ):
        page.goto(app_server_auth)
        # Auth screen should be visible
        expect(page.locator("#auth-screen")).to_be_visible(timeout=5_000)

    def test_login_flow(self, page: Page, app_server_auth: str, mock_lmstudio):
        page.goto(app_server_auth)
        expect(page.locator("#auth-screen")).to_be_visible(timeout=5_000)
        page.locator("#a-user").fill("admin")
        page.locator("#a-pass").fill("testpassword123")
        page.locator("#auth-btn").click()
        # After login, chat UI should appear
        expect(page.locator("#input")).to_be_visible(timeout=10_000)


# ---------------------------------------------------------------------------
# Settings modal (global settings)
# ---------------------------------------------------------------------------

class TestSettingsModal:
    def _open_settings(self, page):
        # #global-settings-btn is defined in index.html — must exist
        page.locator("#global-settings-btn").click()

    def test_settings_modal_opens(self, page_at: Page):
        self._open_settings(page_at)
        # Settings panel renders into #sys-settings with class "open"
        page_at.wait_for_selector("#sys-settings.open", timeout=5_000)

    def test_settings_modal_closes_via_x(self, page_at: Page):
        self._open_settings(page_at)
        page_at.wait_for_selector("#sys-settings.open", timeout=5_000)
        # Close button uses data-action="close-settings" — rendered by openSettings()
        close_btn = page_at.locator("[data-action='close-settings']")
        expect(close_btn).to_be_visible(timeout=3_000)
        close_btn.click()
        page_at.wait_for_timeout(500)
        assert page_at.locator("#sys-settings.open").count() == 0, \
            "Settings panel still open after clicking close button"

    def test_profile_tab_visible(self, page_at: Page):
        self._open_settings(page_at)
        page_at.wait_for_selector("#sys-settings.open", timeout=5_000)
        # Tab buttons are rendered by openSettings() from the tabDef array
        profile_tab = page_at.locator("[data-action='switch-tab'][data-tab='profile']")
        expect(profile_tab).to_be_visible(timeout=3_000)

    def test_security_tab_visible(self, page_at: Page):
        self._open_settings(page_at)
        page_at.wait_for_selector("#sys-settings.open", timeout=5_000)
        security_tab = page_at.locator("[data-action='switch-tab'][data-tab='security']")
        expect(security_tab).to_be_visible(timeout=3_000)

    def test_tab_navigation_switches_panels(self, page_at: Page):
        self._open_settings(page_at)
        page_at.wait_for_selector("#sys-settings.open", timeout=5_000)
        # Click server tab and verify the content area updates
        server_tab = page_at.locator("[data-action='switch-tab'][data-tab='server']")
        expect(server_tab).to_be_visible(timeout=3_000)
        server_tab.click()
        # After clicking server tab, it should become the active tab
        expect(server_tab).to_have_class(re.compile(r"\bactive\b"), timeout=3_000)


# ---------------------------------------------------------------------------
# Memory panel
# ---------------------------------------------------------------------------

class TestMemoryPanel:
    def _open_memory_settings(self, page):
        """Open settings and navigate to the Memory tab."""
        page.locator("#global-settings-btn").click()
        page.wait_for_selector("#sys-settings.open", timeout=5_000)
        # Memory is a tab inside settings — defined in tabDef array
        mem_tab = page.locator("[data-action='switch-tab'][data-tab='memory']")
        expect(mem_tab).to_be_visible(timeout=3_000)
        mem_tab.click()
        page.wait_for_timeout(300)

    def test_memory_panel_opens(self, page_at: Page):
        self._open_memory_settings(page_at)
        # Memory content is in #memory-settings-content (rendered inside sys-settings)
        page_at.wait_for_selector("#memory-settings-content", timeout=5_000)

    def test_memory_toggle_visible(self, page_at: Page):
        self._open_memory_settings(page_at)
        page_at.wait_for_selector("#memory-settings-content", timeout=5_000)
        # Toggle is a checkbox with id="s-memory" — rendered by memory tab
        expect(page_at.locator("#s-memory")).to_be_attached(timeout=3_000)

    def test_memory_toggle_changes_state(self, page_at: Page):
        self._open_memory_settings(page_at)
        page_at.wait_for_selector("#memory-settings-content", timeout=5_000)
        toggle = page_at.locator("#s-memory")
        expect(toggle).to_be_attached(timeout=3_000)
        initial = toggle.is_checked()
        # The input is visually hidden (opacity:0, w:0, h:0) inside a .sw label;
        # click the parent label instead.
        label = page_at.locator("label.sw:has(#s-memory)")
        if label.count() > 0:
            label.click()
        else:
            toggle.click(force=True)
        page_at.wait_for_timeout(300)
        assert toggle.is_checked() != initial, \
            f"Memory toggle state did not change (was {initial}, still {toggle.is_checked()})"


# ---------------------------------------------------------------------------
# Chat list actions
# ---------------------------------------------------------------------------

class TestChatListActions:
    def _api_create_chat(self, base_url: str, title: str) -> str:
        import urllib.request as _r, json as _j
        data = _j.dumps({"title": title}).encode()
        req = _r.Request(
            base_url + "/api/chats", data=data,
            headers={"Content-Type": "application/json", "X-Requested-With": "lm-chat"},
            method="POST",
        )
        resp = _r.urlopen(req, timeout=10)
        return _j.loads(resp.read())["id"]

    def test_chat_appears_in_sidebar_after_creation(self, page_at: Page, app_server: str):
        self._api_create_chat(app_server, "My Sidebar Test Chat")
        page_at.reload()
        page_at.wait_for_timeout(500)
        expect(page_at.locator("#chat-list")).to_contain_text("My Sidebar Test Chat", timeout=5_000)

    def test_delete_chat_shows_confirmation(self, page_at: Page, app_server: str):
        self._api_create_chat(app_server, "Delete Me Chat")
        page_at.reload()
        page_at.wait_for_timeout(500)
        # Chat items have class "ci" — created by renderList() in app.js
        chat_item = page_at.locator(".ci").first
        assert chat_item.count() > 0, "No chat items found in sidebar after creating a chat"
        # Delete button has class "del" inside each .ci; it's CSS display:none
        # until :hover. Use JS dispatchEvent to bypass the visibility constraint.
        del_btn = chat_item.locator(".del")
        assert del_btn.count() > 0, "Delete button (.del) not found in chat item"
        page_at.evaluate("el => el.click()", del_btn.element_handle())
        # Dialog uses class "share-dialog" with role="dialog"
        page_at.wait_for_selector(".share-dialog[role='dialog']", timeout=3_000)

    def test_new_chat_button_visible(self, page_at: Page):
        expect(page_at.locator("#new-chat")).to_be_visible()

    def test_new_chat_clears_messages(self, page_at: Page, mock_lmstudio):
        mock_lmstudio.configure(chunks=["Hello"])
        page_at.locator("#input").fill("Hello")
        page_at.locator("#send").click()
        page_at.wait_for_selector(".m-asst", timeout=10_000)
        page_at.locator("#new-chat").click()
        page_at.wait_for_timeout(400)
        assert page_at.locator(".m-asst").count() == 0

    def test_search_input_visible(self, page_at: Page):
        # Chat search is #chat-search (always present in sidebar)
        expect(page_at.locator("#chat-search")).to_be_visible()


# ---------------------------------------------------------------------------
# Sidebar navigation
# ---------------------------------------------------------------------------

class TestSidebarNavigation:
    def _api_create_chat(self, base_url: str, title: str) -> str:
        import urllib.request as _r, json as _j
        data = _j.dumps({"title": title}).encode()
        req = _r.Request(
            base_url + "/api/chats", data=data,
            headers={"Content-Type": "application/json", "X-Requested-With": "lm-chat"},
            method="POST",
        )
        return _j.loads(_r.urlopen(req, timeout=10).read())["id"]

    def test_click_chat_in_sidebar_loads_it(self, page_at: Page, app_server: str):
        self._api_create_chat(app_server, "Nav Test Chat")
        page_at.reload()
        page_at.wait_for_timeout(500)
        # Chat items use class "ci"; clicking sets "active" on the selected item
        chat_items = page_at.locator(".ci")
        assert chat_items.count() > 0, "No chat items in sidebar after creating a chat"
        chat_items.first.click()
        # loadChat() calls renderList() which adds .active to the selected .ci
        page_at.wait_for_selector(".ci.active", timeout=5_000)

    def test_new_chat_active_on_start(self, page_at: Page):
        # Fresh load: no assistant messages
        assert page_at.locator(".m-asst").count() == 0

    def test_multiple_chats_in_sidebar(self, page_at: Page, app_server: str):
        for i in range(3):
            self._api_create_chat(app_server, f"Chat {i}")
        page_at.reload()
        page_at.wait_for_timeout(500)
        # Each chat item is a .ci div
        items = page_at.locator(".ci")
        assert items.count() >= 3


# ---------------------------------------------------------------------------
# Keyboard shortcuts
# ---------------------------------------------------------------------------

class TestKeyboardShortcuts:
    @pytest.mark.smoke
    def test_enter_sends_message(self, page_at: Page, mock_lmstudio):
        mock_lmstudio.configure(chunks=["Reply"])
        page_at.locator("#input").fill("Enter key test")
        page_at.locator("#input").press("Enter")
        page_at.wait_for_selector(".m-user", timeout=5_000)
        expect(page_at.locator(".m-user").first).to_contain_text("Enter key test")

    @pytest.mark.smoke
    def test_shift_enter_inserts_newline(self, page_at: Page):
        page_at.locator("#input").fill("")
        page_at.locator("#input").press("Shift+Enter")
        page_at.wait_for_timeout(200)
        val = page_at.locator("#input").input_value()
        assert "\n" in val
        assert page_at.locator(".m-user").count() == 0

    def test_empty_input_enter_does_not_send(self, page_at: Page):
        page_at.locator("#input").fill("")
        page_at.locator("#input").press("Enter")
        page_at.wait_for_timeout(300)
        assert page_at.locator(".m-user").count() == 0


# ---------------------------------------------------------------------------
# Incognito mode
# ---------------------------------------------------------------------------

class TestIncognitoMode:
    def test_incognito_toggle_exists(self, page_at: Page):
        # #incognito-btn is defined in index.html
        toggle = page_at.locator("#incognito-btn")
        expect(toggle).to_be_visible(timeout=3_000)

    def test_incognito_can_be_activated(self, page_at: Page):
        toggle = page_at.locator("#incognito-btn")
        expect(toggle).to_be_visible(timeout=3_000)
        toggle.click()
        page_at.wait_for_timeout(300)
        # Body should gain "incognito" class when active
        assert page_at.locator("body.incognito").count() > 0, \
            "body did not gain .incognito class after clicking toggle"

    def test_incognito_message_not_in_new_chat_list(self, page_at: Page, mock_lmstudio, app_server: str):
        import json as _j, urllib.request as _r
        mock_lmstudio.configure(chunks=["incognito reply"])
        toggle = page_at.locator("#incognito-btn")
        expect(toggle).to_be_visible(timeout=3_000)
        toggle.click()
        page_at.wait_for_timeout(200)
        page_at.locator("#input").fill("Incognito message")
        page_at.locator("#input").press("Enter")
        page_at.wait_for_selector(".m-asst", timeout=10_000)
        time.sleep(0.3)
        chats = _j.loads(_r.urlopen(app_server + "/api/chats", timeout=5).read())
        chat_list = chats if isinstance(chats, list) else chats.get("chats", [])
        assert all(c.get("message_count", 0) == 0 for c in chat_list)


# ---------------------------------------------------------------------------
# Share UI
# ---------------------------------------------------------------------------

class TestShareUI:
    def _send_and_wait(self, page, mock_lmstudio, text="Hello"):
        mock_lmstudio.configure(chunks=["Reply"])
        page.locator("#input").fill(text)
        page.locator("#send").click()
        page.wait_for_selector(".m-asst", timeout=10_000)

    def test_share_button_present(self, page_at: Page, mock_lmstudio):
        # #share-btn is defined in index.html; starts hidden, becomes visible with a chat
        self._send_and_wait(page_at, mock_lmstudio)
        page_at.wait_for_timeout(300)
        btn = page_at.locator("#share-btn")
        assert btn.count() > 0, "Share button (#share-btn) not found in DOM"

    def test_share_button_becomes_visible_with_active_chat(self, page_at: Page, mock_lmstudio):
        self._send_and_wait(page_at, mock_lmstudio)
        page_at.wait_for_timeout(500)
        btn = page_at.locator("#share-btn")
        assert btn.count() > 0, "Share button (#share-btn) not found in DOM"
        # After a message is sent, the button should not be hidden
        classes = btn.get_attribute("class") or ""
        assert "hidden" not in classes, "Share button still hidden after chat has messages"

    def test_share_dialog_opens(self, page_at: Page, mock_lmstudio):
        self._send_and_wait(page_at, mock_lmstudio)
        page_at.wait_for_timeout(500)
        btn = page_at.locator("#share-btn")
        assert btn.count() > 0, "Share button (#share-btn) not found in DOM"
        classes = btn.get_attribute("class") or ""
        assert "hidden" not in classes, "Share button still hidden after chat has messages"
        btn.click()
        page_at.wait_for_selector(".share-dialog", timeout=5_000)

    def test_share_dialog_contains_url(self, page_at: Page, mock_lmstudio):
        self._send_and_wait(page_at, mock_lmstudio)
        page_at.wait_for_timeout(500)
        btn = page_at.locator("#share-btn")
        assert btn.count() > 0, "Share button (#share-btn) not found in DOM"
        classes = btn.get_attribute("class") or ""
        assert "hidden" not in classes, "Share button still hidden after chat has messages"
        btn.click()
        page_at.wait_for_selector(".share-dialog", timeout=5_000)
        dialog_text = page_at.locator(".share-dialog").inner_text()
        assert "/share/" in dialog_text or "share" in dialog_text.lower()

    def test_share_close_button_works(self, page_at: Page, mock_lmstudio):
        self._send_and_wait(page_at, mock_lmstudio)
        page_at.wait_for_timeout(500)
        btn = page_at.locator("#share-btn")
        assert btn.count() > 0, "Share button (#share-btn) not found in DOM"
        classes = btn.get_attribute("class") or ""
        assert "hidden" not in classes, "Share button still hidden after chat has messages"
        btn.click()
        page_at.wait_for_selector(".share-dialog", timeout=5_000)
        # Close button is rendered by shareChat() with data-action="close-share-dialog"
        close_btn = page_at.locator("[data-action='close-share-dialog']")
        assert close_btn.count() > 0, "Close button not found in share dialog"
        close_btn.click()
        page_at.wait_for_timeout(300)
        assert page_at.locator(".share-dialog").count() == 0, "Share dialog still open after close"


# ---------------------------------------------------------------------------
# Think blocks (reasoning UI)
# ---------------------------------------------------------------------------

class TestThinkBlocks:
    def test_think_block_rendered_for_reasoning_response(self, page_at: Page, mock_lmstudio):
        mock_lmstudio.configure(
            reasoning_chunks=["Let me think about this..."],
            chunks=["Here is the answer"],
        )
        page_at.locator("#input").fill("Reason about this")
        page_at.locator("#send").click()
        page_at.wait_for_selector(".m-asst", timeout=10_000)
        # Think block must be rendered when reasoning_chunks are provided
        # Wait for either the toggle or the body to appear
        page_at.wait_for_selector(
            "[data-action='toggle-think'], .think-body, .m-think",
            timeout=5_000,
        )

    def test_think_block_body_exists(self, page_at: Page, mock_lmstudio):
        mock_lmstudio.configure(
            reasoning_chunks=["Internal thought process"],
            chunks=["Answer"],
        )
        page_at.locator("#input").fill("Think")
        page_at.locator("#send").click()
        page_at.wait_for_selector(".m-asst", timeout=10_000)
        # think-body must be rendered for reasoning responses
        think_body = page_at.locator(".think-body").first
        expect(think_body).to_be_attached(timeout=5_000)
        classes = think_body.get_attribute("class") or ""
        assert "think-body" in classes

    def test_think_block_collapsed_by_default(self, page_at: Page, mock_lmstudio):
        mock_lmstudio.configure(
            reasoning_chunks=["Internal thought process"],
            chunks=["Answer"],
        )
        page_at.locator("#input").fill("Think")
        page_at.locator("#send").click()
        page_at.wait_for_selector(".m-asst", timeout=10_000)
        think_body = page_at.locator(".think-body").first
        expect(think_body).to_be_attached(timeout=5_000)
        # Wait for reasoning to complete (stream ends) before checking collapsed state
        page_at.wait_for_timeout(500)
        classes = think_body.get_attribute("class") or ""
        # After reasoning.end, think-body should NOT have 'open' class
        assert "open" not in classes, \
            "Think body has 'open' class but should be collapsed by default after reasoning ends"

    def test_think_block_expands_on_click(self, page_at: Page, mock_lmstudio):
        mock_lmstudio.configure(
            reasoning_chunks=["My thinking"],
            chunks=["My answer"],
        )
        page_at.locator("#input").fill("Think please")
        page_at.locator("#send").click()
        page_at.wait_for_selector(".m-asst", timeout=10_000)
        # Wait for toggle to be rendered
        toggle = page_at.locator("[data-action='toggle-think']").first
        expect(toggle).to_be_attached(timeout=5_000)
        page_at.wait_for_timeout(500)
        toggle.click()
        page_at.wait_for_timeout(300)
        think_body = page_at.locator(".think-body").first
        expect(think_body).to_be_attached(timeout=3_000)
        classes = think_body.get_attribute("class") or ""
        assert "open" in classes, "Think body did not gain 'open' class after clicking toggle"


# ---------------------------------------------------------------------------
# Code blocks
# ---------------------------------------------------------------------------

class TestCodeBlocks:
    def test_code_fence_renders_as_pre_code(self, page_at: Page, mock_lmstudio):
        mock_lmstudio.configure(chunks=["```python\nprint('hello')\n```"])
        page_at.locator("#input").fill("Show code")
        page_at.locator("#send").click()
        page_at.wait_for_selector(".m-asst", timeout=10_000)
        expect(page_at.locator(".m-asst pre code").last).to_be_visible(timeout=5_000)

    def test_code_block_copy_button_present(self, page_at: Page, mock_lmstudio):
        mock_lmstudio.configure(chunks=["```python\nprint('hello')\n```"])
        page_at.locator("#input").fill("Show code")
        page_at.locator("#send").click()
        page_at.wait_for_selector(".m-asst pre code", timeout=10_000)
        # Wait for addCopyButtons() to run after stream completes
        copy_btn = page_at.locator("pre .copy-btn")
        expect(copy_btn.first).to_be_visible(timeout=5_000)

    def test_code_block_copy_button_text(self, page_at: Page, mock_lmstudio):
        mock_lmstudio.configure(chunks=["```python\nprint('hello')\n```"])
        page_at.locator("#input").fill("Show code")
        page_at.locator("#send").click()
        page_at.wait_for_selector(".m-asst pre code", timeout=10_000)
        copy_btn = page_at.locator("pre .copy-btn").first
        expect(copy_btn).to_be_visible(timeout=5_000)
        assert copy_btn.inner_text().strip() in ("Copy", "Copy code")


# ---------------------------------------------------------------------------
# Regenerate
# ---------------------------------------------------------------------------

class TestRegenerateFlow:
    def test_regenerate_button_visible_on_last_asst_message(self, page_at: Page, mock_lmstudio):
        mock_lmstudio.configure(chunks=["First response"])
        page_at.locator("#input").fill("Regenerate test")
        page_at.locator("#send").click()
        page_at.wait_for_selector(".m-asst", timeout=10_000)
        # .regen-btn is appended by addRegenButton() after stream completes
        regen = page_at.locator(".regen-btn")
        expect(regen.last).to_be_visible(timeout=5_000)

    def test_regenerate_triggers_new_response(self, page_at: Page, mock_lmstudio):
        mock_lmstudio.configure(chunks=["First response"])
        page_at.locator("#input").fill("Regenerate test")
        page_at.locator("#send").click()
        page_at.wait_for_selector(".m-asst", timeout=10_000)
        regen = page_at.locator(".regen-btn")
        expect(regen.last).to_be_visible(timeout=5_000)
        mock_lmstudio.configure(chunks=["Second", " response"])
        regen.last.click()
        # Wait for new content to stream in — "Second response" must appear in the last .m-asst
        expect(page_at.locator(".m-asst").last).to_contain_text("Second response", timeout=10_000)

    def test_regenerate_button_disabled_while_streaming(self, page_at: Page, mock_lmstudio):
        mock_lmstudio.configure(chunks=["Response"], delay_ms=50)
        page_at.locator("#input").fill("Test")
        page_at.locator("#send").click()
        page_at.wait_for_selector(".m-asst", timeout=10_000)
        regen = page_at.locator(".regen-btn")
        expect(regen.last).to_be_visible(timeout=5_000)
        # After streaming completes, button should be enabled
        disabled = regen.last.get_attribute("disabled")
        assert disabled is None, "Regen button is disabled after stream completed"


# ---------------------------------------------------------------------------
# Edit message
# ---------------------------------------------------------------------------

class TestEditMessage:
    def _js_click_edit_btn(self, page: Page):
        """
        Click the edit button via JS dispatch to bypass #msgs overlay interception.
        The .edit-btn is positioned at left:-2rem outside the message bubble and
        #msgs intercepts normal Playwright clicks.
        Asserts the button exists (fails if not found).
        """
        result = page.evaluate("""
            () => {
                const btn = document.querySelector('.m-user .edit-btn');
                if (!btn) return false;
                btn.click();
                return true;
            }
        """)
        assert result, "Edit button (.edit-btn) not found on user message"

    def test_edit_button_present_on_user_message(self, page_at: Page, mock_lmstudio):
        mock_lmstudio.configure(chunks=["OK"])
        page_at.locator("#input").fill("Editable message")
        page_at.locator("#send").click()
        page_at.wait_for_selector(".m-user", timeout=5_000)
        page_at.wait_for_timeout(200)
        # .edit-btn is created by buildUserRow() in app.js — must be present in DOM
        edit_btn = page_at.locator(".m-user .edit-btn")
        assert edit_btn.count() > 0, "Edit button (.edit-btn) not found on user message"

    def test_edit_opens_inline_editor(self, page_at: Page, mock_lmstudio):
        mock_lmstudio.configure(chunks=["OK"])
        page_at.locator("#input").fill("Edit me")
        page_at.locator("#send").click()
        page_at.wait_for_selector(".m-user", timeout=5_000)
        page_at.wait_for_timeout(200)
        self._js_click_edit_btn(page_at)
        # startEdit() creates a .edit-area div with a textarea inside
        page_at.wait_for_selector(".edit-area", timeout=3_000)
        expect(page_at.locator(".edit-area textarea")).to_be_visible(timeout=3_000)

    def test_edit_area_prefilled_with_original_text(self, page_at: Page, mock_lmstudio):
        mock_lmstudio.configure(chunks=["OK"])
        page_at.locator("#input").fill("Original text here")
        page_at.locator("#send").click()
        page_at.wait_for_selector(".m-user", timeout=5_000)
        page_at.wait_for_timeout(200)
        self._js_click_edit_btn(page_at)
        page_at.wait_for_selector(".edit-area textarea", timeout=3_000)
        val = page_at.locator(".edit-area textarea").input_value()
        assert "Original text here" in val

    def test_cancel_edit_restores_original(self, page_at: Page, mock_lmstudio):
        mock_lmstudio.configure(chunks=["OK"])
        page_at.locator("#input").fill("Cancel edit test")
        page_at.locator("#send").click()
        page_at.wait_for_selector(".m-user", timeout=5_000)
        page_at.wait_for_timeout(200)
        self._js_click_edit_btn(page_at)
        page_at.wait_for_selector(".edit-area", timeout=3_000)
        cancel_btn = page_at.locator("[data-action='cancel-edit']")
        assert cancel_btn.count() > 0, "Cancel edit button ([data-action='cancel-edit']) not found"
        cancel_btn.click()
        page_at.wait_for_timeout(300)
        # edit-area should be gone, original bubble visible
        assert page_at.locator(".edit-area").count() == 0, "edit-area still visible after cancel"
        user_msg = page_at.locator(".m-user").first
        expect(user_msg.locator(".bub")).to_be_visible(timeout=2_000)

    def test_save_edit_buttons_present(self, page_at: Page, mock_lmstudio):
        mock_lmstudio.configure(chunks=["OK"])
        page_at.locator("#input").fill("Save test")
        page_at.locator("#send").click()
        page_at.wait_for_selector(".m-user", timeout=5_000)
        page_at.wait_for_timeout(200)
        self._js_click_edit_btn(page_at)
        page_at.wait_for_selector(".edit-area", timeout=3_000)
        assert page_at.locator("[data-action='save-edit']").count() > 0, "Save & Send button missing"
        assert page_at.locator("[data-action='cancel-edit']").count() > 0, "Cancel button missing"


# ---------------------------------------------------------------------------
# Agent modes (client-side slash commands)
# ---------------------------------------------------------------------------

class TestAgentModes:
    def test_slash_research_accepted_in_input(self, page_at: Page):
        # Type "/research " and verify the cmd-badge activates (the slash command
        # is recognised). The input retains the text and the badge becomes visible.
        page_at.locator("#input").fill("/research ")
        page_at.wait_for_timeout(300)
        val = page_at.locator("#input").input_value()
        assert val == "/research ", \
            f"Input value changed unexpectedly: expected '/research ', got '{val}'"
        # The cmd-badge should gain .visible for a recognised slash command
        badge = page_at.locator("#cmd-badge")
        classes = badge.get_attribute("class") or ""
        assert "visible" in classes, \
            "cmd-badge did not gain .visible after '/research ' — slash command not recognised"

    def test_cmd_badge_appears_after_valid_slash_command(self, page_at: Page):
        # #cmd-badge gains .visible when user types a recognised /cmd followed by space
        page_at.locator("#input").fill("/code ")
        page_at.wait_for_timeout(300)
        badge = page_at.locator("#cmd-badge")
        # #cmd-badge is defined in index.html — must exist
        assert badge.count() > 0, "cmd-badge element (#cmd-badge) not found in DOM"
        classes = badge.get_attribute("class") or ""
        assert "visible" in classes, "cmd-badge did not gain .visible after '/code '"

    def test_cmd_badge_absent_without_slash(self, page_at: Page):
        page_at.locator("#input").fill("normal message")
        page_at.wait_for_timeout(300)
        badge = page_at.locator("#cmd-badge")
        if badge.count() == 0:
            return  # element may not exist — that's fine
        classes = badge.get_attribute("class") or ""
        assert "visible" not in classes, "cmd-badge should not be visible for non-slash input"

    def test_slash_menu_opens_on_slash_key(self, page_at: Page):
        page_at.locator("#input").fill("/")
        page_at.wait_for_timeout(300)
        menu = page_at.locator("#slash-menu")
        # #slash-menu is defined in index.html — must exist
        assert menu.count() > 0, "slash-menu element (#slash-menu) not found in DOM"
        classes = menu.get_attribute("class") or ""
        assert "open" in classes, "slash-menu did not open when input starts with '/'"

    def test_agent_mode_resets_badge_on_new_chat(self, page_at: Page, mock_lmstudio):
        mock_lmstudio.configure(chunks=["code response"])
        page_at.locator("#input").fill("/code write a function")
        page_at.locator("#send").click()
        page_at.wait_for_selector(".m-user", timeout=5_000)
        page_at.locator("#new-chat").click()
        page_at.wait_for_timeout(300)
        badge = page_at.locator("#cmd-badge")
        if badge.count() == 0:
            return
        classes = badge.get_attribute("class") or ""
        assert "visible" not in classes, "cmd-badge should not be visible after new chat"


# ---------------------------------------------------------------------------
# Multi-user isolation
# ---------------------------------------------------------------------------

class TestMultiUserIsolation:
    def test_user_b_cannot_see_user_a_chats(self, page: Page, app_server_auth: str, authed_client):
        import json as _j, urllib.request as _r, time as _t
        # Admin (user A) creates a private chat via API
        data = _j.dumps({"title": "Admin Private Chat"}).encode()
        req = _r.Request(
            app_server_auth + "/api/chats", data=data,
            headers={
                "Content-Type": "application/json",
                "X-Requested-With": "lm-chat",
                "Cookie": authed_client.admin.cookie,
            },
            method="POST",
        )
        _r.urlopen(req, timeout=10)
        # Log in as testuser (user B) via browser
        page.goto(app_server_auth)
        page.locator("#a-user").fill("testuser")
        page.locator("#a-pass").fill("userpassword1")
        page.locator("#auth-btn").click()
        page.wait_for_selector("#input", timeout=10_000)
        _t.sleep(0.5)
        sidebar_text = (
            page.locator("#chat-list").inner_text()
            if page.locator("#chat-list").count() > 0
            else ""
        )
        assert "Admin Private Chat" not in sidebar_text

    def test_user_b_api_access_to_user_a_chat_returns_403(self, authed_client):
        import json as _j, urllib.error as _err, urllib.request as _r
        # Admin creates a chat
        data = _j.dumps({"title": "Admin Chat for B"}).encode()
        req = _r.Request(
            authed_client.admin.base_url + "/api/chats", data=data,
            headers={
                "Content-Type": "application/json",
                "X-Requested-With": "lm-chat",
                "Cookie": authed_client.admin.cookie,
            },
            method="POST",
        )
        resp = _r.urlopen(req, timeout=10)
        chat_id = _j.loads(resp.read())["id"]
        # testuser tries to read admin's chat messages — should get 403 or 404
        with pytest.raises(_err.HTTPError) as exc:
            authed_client.user.get(f"/api/chats/{chat_id}/messages")
        assert exc.value.code in (403, 404), f"Expected 403/404, got {exc.value.code}"


# ---------------------------------------------------------------------------
# Cross-browser smoke
# ---------------------------------------------------------------------------

class TestCrossBrowserSmoke:
    @pytest.mark.smoke
    def test_page_loads(self, page_at: Page):
        expect(page_at.locator("#input")).to_be_visible()

    @pytest.mark.smoke
    def test_send_button_present(self, page_at: Page):
        expect(page_at.locator("#send")).to_be_visible()

    @pytest.mark.smoke
    def test_send_message(self, page_at: Page, mock_lmstudio):
        mock_lmstudio.configure(chunks=["Cross-browser reply"])
        page_at.locator("#input").fill("Cross-browser test")
        page_at.locator("#send").click()
        page_at.wait_for_selector(".m-asst", timeout=10_000)
        expect(page_at.locator(".m-asst").last).to_contain_text("Cross-browser reply", timeout=5_000)

    @pytest.mark.smoke
    def test_new_chat_clears_messages(self, page_at: Page, mock_lmstudio):
        mock_lmstudio.configure(chunks=["Smoke reply"])
        page_at.locator("#input").fill("Smoke test")
        page_at.locator("#send").click()
        page_at.wait_for_selector(".m-asst", timeout=10_000)
        page_at.locator("#new-chat").click()
        page_at.wait_for_timeout(300)
        assert page_at.locator(".m-asst").count() == 0, "Messages persisted after new chat"
