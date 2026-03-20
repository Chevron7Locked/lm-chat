"""
Shared fixtures for lm-chat test suite.

Architecture:
  - mock_lmstudio: in-process stdlib HTTP server replacing LM Studio upstream
  - app_server: server.py subprocess pointed at mock_lmstudio, LM_CHAT_AUTH=false
  - app_server_auth: same but with AUTH=true and a known admin password
  - authed_client: session-authenticated helpers on top of app_server_auth
"""

import hashlib, hmac, json, os, socket, struct, subprocess, sys, tempfile, threading, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CSRF_HEADER = {"X-Requested-With": "lm-chat"}
ADMIN_PASS  = "testpassword123"
ADMIN_USER  = "admin"

CANNED_MODELS = {
    "data": [
        {"id": "test-model", "object": "model"},
    ]
}

CANNED_RESPONSE = {
    "id":          "resp-test-001",
    "object":      "chat.completion",
    "response_id": "resp-test-001",
    # Top-level "content" is checked first by _extract_content — use a plain string
    # so persistence doesn't fail with a TypeError on nested list content.
    "content":     "Hello world",
    "usage":       {"input_tokens": 5, "output_tokens": 3},
}

# ---------------------------------------------------------------------------
# TOTP helper (stdlib only — no pyotp)
# ---------------------------------------------------------------------------

def generate_totp(secret: bytes, offset: int = 0) -> str:
    """Generate a valid TOTP code. offset shifts by 30-second intervals."""
    counter = struct.pack(">Q", int(time.time()) // 30 + offset)
    mac = hmac.new(secret, counter, hashlib.sha1).digest()
    idx = mac[-1] & 0x0F
    code = struct.unpack(">I", mac[idx : idx + 4])[0] & 0x7FFFFFFF
    return f"{code % 1_000_000:06d}"


# ---------------------------------------------------------------------------
# Free port helper
# ---------------------------------------------------------------------------

def _free_port() -> int:
    """Return an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Mock LM Studio HTTP server
# ---------------------------------------------------------------------------

class _MockConfig:
    """Thread-safe configuration for mock LM Studio behaviour."""

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.chunks: list[str]     = ["Hello", " world"]
            self.disconnect_after: int | None = None  # drop after N chunks
            self.delay_ms: int         = 0
            self.status_code: int      = 200
            self.validate_schema: bool = True
            self.last_request: dict | None = None
            self.call_count: int          = 0
            self.reasoning_chunks: list   = []
            self.tool_calls: list         = []

    def configure(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def snapshot(self):
        with self._lock:
            return {
                "chunks":           self.chunks,
                "disconnect_after": self.disconnect_after,
                "delay_ms":         self.delay_ms,
                "status_code":      self.status_code,
                "validate_schema":  self.validate_schema,
                "reasoning_chunks": self.reasoning_chunks,
                "tool_calls":       self.tool_calls,
            }


class _MockLMStudioHandler(BaseHTTPRequestHandler):
    config: _MockConfig   # injected by the fixture

    def log_message(self, *args):
        pass  # suppress request logging during tests

    def do_GET(self):
        if self.path == "/api/v1/models":
            body = json.dumps(CANNED_MODELS).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/api/v1/chat":
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        # Capture request body and call count for test assertions
        try:
            _parsed = json.loads(raw) if raw else {}
        except Exception:
            _parsed = {}
        with self.config._lock:
            self.config.last_request = _parsed
            self.config.call_count += 1

        cfg = self.config.snapshot()

        if cfg["validate_schema"]:
            try:
                data = json.loads(raw)
                assert "input" in data or "messages" in data, "missing input/messages"
            except Exception as e:
                self.send_error(400, str(e))
                return

        if cfg["status_code"] != 200:
            self.send_error(cfg["status_code"])
            return

        # Detect whether the client wants streaming
        try:
            req_data = json.loads(raw)
        except Exception:
            req_data = {}
        is_stream = req_data.get("stream", False)

        if is_stream:
            self._stream_response(cfg)
        else:
            self._json_response(cfg)

    def _json_response(self, cfg):
        body = json.dumps(CANNED_RESPONSE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream_response(self, cfg):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # No Content-Length and no Transfer-Encoding: chunked — using HTTP/1.0
        # connection-close semantics so urllib on the server side reads raw bytes
        # line-by-line until the connection closes (avoids broken chunk decoding).
        self.end_headers()

        chunks = cfg["chunks"]
        disconnect_after = cfg["disconnect_after"]
        delay_ms = cfg["delay_ms"]

        # Emit reasoning.delta events before message chunks
        for _rc in cfg["reasoning_chunks"]:
            _line = (
                f"event: reasoning.delta\n"
                f"data: {json.dumps({'content': _rc})}\n\n"
            ).encode()
            try:
                self.wfile.write(_line)
                self.wfile.flush()
            except BrokenPipeError:
                return

        # Use LM Studio native SSE event format (event: + data: lines)
        for i, text in enumerate(chunks):
            if disconnect_after is not None and i >= disconnect_after:
                break  # simulate mid-stream disconnect
            line = (
                f"event: message.delta\n"
                f"data: {json.dumps({'content': text})}\n\n"
            ).encode()
            try:
                self.wfile.write(line)
                self.wfile.flush()
            except BrokenPipeError:
                return
            if delay_ms > 0:
                time.sleep(delay_ms / 1000)

        # Emit tool_call event sequences after message chunks
        for _tc in cfg["tool_calls"]:
            try:
                self.wfile.write((
                    f"event: tool_call.start\n"
                    f"data: {json.dumps({'id': _tc.get('id','tc1'), 'tool': _tc.get('tool','test_tool'), 'arguments': ''})}\n\n"
                ).encode())
                self.wfile.flush()
                self.wfile.write((
                    f"event: tool_call.arguments\n"
                    f"data: {json.dumps({'argumentsDelta': _tc.get('arguments', '{}')})}\n\n"
                ).encode())
                self.wfile.flush()
                if _tc.get("error"):
                    self.wfile.write((
                        f"event: tool_call.failure\n"
                        f"data: {json.dumps({'id': _tc.get('id','tc1'), 'error': _tc.get('error','Tool failed')})}\n\n"
                    ).encode())
                else:
                    self.wfile.write((
                        f"event: tool_call.success\n"
                        f"data: {json.dumps({'id': _tc.get('id','tc1'), 'output': str(_tc.get('output','result'))})}\n\n"
                    ).encode())
                self.wfile.flush()
            except BrokenPipeError:
                return

        # chat.end event carries response_id and usage
        end_event = {
            "response_id": "resp-mock-001",
            "usage": {"input_tokens": 10, "output_tokens": len(chunks)},
        }
        try:
            self.wfile.write(
                f"event: chat.end\n"
                f"data: {json.dumps(end_event)}\n\n".encode()
            )
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except BrokenPipeError:
            pass


# ---------------------------------------------------------------------------
# mock_lmstudio fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def mock_lmstudio():
    """
    Session-scoped mock LM Studio server.
    Tests reset its config via mock_lmstudio.reset() or mock_lmstudio.configure().
    """
    config = _MockConfig()

    class Handler(_MockLMStudioHandler):
        pass

    Handler.config = config
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    config.url = f"http://127.0.0.1:{port}"
    yield config

    server.shutdown()


@pytest.fixture(autouse=True)
def _reset_mock_lmstudio(mock_lmstudio):
    """Function-scoped auto-use: reset mock config before every test."""
    mock_lmstudio.reset()
    yield


# ---------------------------------------------------------------------------
# app_server fixture (AUTH=false)
# ---------------------------------------------------------------------------

def _wait_for_health(base_url: str, timeout: float = 25.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/health", timeout=2) as r:
                if r.status in (200, 503):  # 503 means LM Studio down but server is up
                    return
        except Exception:
            pass
        time.sleep(0.15)
    raise RuntimeError(f"Server at {base_url} did not become healthy within {timeout}s")


@pytest.fixture
def app_server(mock_lmstudio, tmp_path):
    """
    Start server.py with AUTH=false, pointed at mock_lmstudio.
    Yields base_url string. Function-scoped = fresh DB per test.
    """
    port = _free_port()
    time.sleep(0.05)  # brief pause so OS can fully release the port
    db_path = str(tmp_path / "test.db")
    env = {
        **os.environ,
        "PORT":                     str(port),
        "LMSTUDIO_URL":             mock_lmstudio.url,
        "LM_CHAT_AUTH":             "false",
        "LM_CHAT_DB":               db_path,
        "LM_CHAT_LOGS":             str(tmp_path / "logs"),
        "COVERAGE_PROCESS_START":   str(Path(__file__).parent.parent / "pyproject.toml"),
        # tests/ first (for sitecustomize.py), then repo root (for server.py imports)
        "PYTHONPATH": (
            str(Path(__file__).parent)
            + os.pathsep + str(Path(__file__).parent.parent)
            + os.pathsep + os.environ.get("PYTHONPATH", "")
        ),
    }
    proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(os.path.dirname(__file__)), "server.py")],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_health(base_url)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def app_server_auth(mock_lmstudio, tmp_path):
    """
    Start server.py with AUTH=true and a known admin password.
    Function-scoped = fresh DB + clean rate-limit counters per test.
    """
    port = _free_port()
    time.sleep(0.05)  # brief pause so OS can fully release the port
    db_path = str(tmp_path / "test.db")
    env = {
        **os.environ,
        "PORT":                     str(port),
        "LMSTUDIO_URL":             mock_lmstudio.url,
        "LM_CHAT_AUTH":             "true",
        "LM_CHAT_ADMIN_PASS":       ADMIN_PASS,
        "LM_CHAT_ADMIN_USER":       ADMIN_USER,
        "LM_CHAT_DB":               db_path,
        "LM_CHAT_LOGS":             str(tmp_path / "logs"),
        "COVERAGE_PROCESS_START":   str(Path(__file__).parent.parent / "pyproject.toml"),
        # tests/ first (for sitecustomize.py), then repo root (for server.py imports)
        "PYTHONPATH": (
            str(Path(__file__).parent)
            + os.pathsep + str(Path(__file__).parent.parent)
            + os.pathsep + os.environ.get("PYTHONPATH", "")
        ),
    }
    proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(os.path.dirname(__file__)), "server.py")],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_health(base_url)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

class _Client:
    """Thin urllib wrapper with session cookie + CSRF header support."""

    def __init__(self, base_url: str, cookie: str = ""):
        self.base_url = base_url
        self.cookie   = cookie

    def _headers(self, extra: dict | None = None, csrf: bool = False) -> dict:
        h = {"Content-Type": "application/json"}
        if self.cookie:
            h["Cookie"] = self.cookie
        if csrf:
            h.update(CSRF_HEADER)
        if extra:
            h.update(extra)
        return h

    def get(self, path: str, headers: dict | None = None) -> urllib.request.Request:
        req = urllib.request.Request(
            self.base_url + path,
            headers=self._headers(headers),
        )
        return urllib.request.urlopen(req, timeout=10)

    def post(self, path: str, body: dict | None = None, headers: dict | None = None, csrf: bool = True):
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=self._headers(headers, csrf=csrf),
            method="POST",
        )
        return urllib.request.urlopen(req, timeout=10)

    def patch(self, path: str, body: dict | None = None, csrf: bool = True):
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=self._headers(None, csrf=csrf),
            method="PATCH",
        )
        return urllib.request.urlopen(req, timeout=10)

    def delete(self, path: str, csrf: bool = True):
        req = urllib.request.Request(
            self.base_url + path,
            headers=self._headers(None, csrf=csrf),
            method="DELETE",
        )
        return urllib.request.urlopen(req, timeout=10)

    def post_raw(self, path: str, body: dict | None = None, headers: dict | None = None):
        """POST without CSRF header (for negative tests)."""
        return self.post(path, body, headers, csrf=False)

    @staticmethod
    def json(response) -> dict:
        return json.loads(response.read())


def _login(base_url: str, username: str, password: str) -> str:
    """Returns the session cookie string."""
    data = json.dumps({"username": username, "password": password}).encode()
    req = urllib.request.Request(
        base_url + "/api/auth/login",
        data=data,
        headers={
            "Content-Type":   "application/json",
            **CSRF_HEADER,
        },
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=10)
    raw_cookie = resp.headers.get("Set-Cookie", "")
    # Extract just the name=value pair (before the first semicolon)
    return raw_cookie.split(";")[0].strip()


# ---------------------------------------------------------------------------
# authed_client fixture
# ---------------------------------------------------------------------------

class AuthedClient:
    """Admin + regular-user clients for auth-enabled tests."""

    def __init__(self, base_url: str):
        self.base_url = base_url
        admin_cookie  = _login(base_url, ADMIN_USER, ADMIN_PASS)
        self.admin    = _Client(base_url, cookie=admin_cookie)

        # Create a regular user via admin
        resp = self.admin.post(
            "/api/auth/invite",
            {"username": "testuser", "password": "userpassword1"},
        )
        assert resp.status == 200
        user_cookie   = _login(base_url, "testuser", "userpassword1")
        self.user     = _Client(base_url, cookie=user_cookie)

    def anon(self) -> _Client:
        return _Client(self.base_url)


@pytest.fixture
def authed_client(app_server_auth) -> AuthedClient:
    return AuthedClient(app_server_auth)


# ---------------------------------------------------------------------------
# Convenience: unauthenticated client against app_server
# ---------------------------------------------------------------------------

@pytest.fixture
def client(app_server) -> _Client:
    return _Client(app_server)


# ---------------------------------------------------------------------------
# Shared test helpers (importable by all test modules)
# ---------------------------------------------------------------------------

def _create_chat(client: "_Client", title: str = "Test Chat") -> str:
    """Create a chat and return its ID."""
    resp = client.post("/api/chats", {"title": title})
    return json.loads(resp.read())["id"]


# ---------------------------------------------------------------------------
# Screenshot on E2E test failure
# ---------------------------------------------------------------------------

@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call" and rep.failed and "page" in item.funcargs:
        page = item.funcargs["page"]
        safe = (
            item.nodeid
            .replace("/", "_").replace("::", "_")
            .replace("[", "_").replace("]", "")
        )
        os.makedirs(os.path.join(os.path.dirname(__file__), "screenshots"), exist_ok=True)
        page.screenshot(path=os.path.join(os.path.dirname(__file__), "screenshots", f"{safe}.png"))


# ---------------------------------------------------------------------------
# Authenticated Playwright fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def page_at_auth(page, app_server_auth):
    """Navigate to auth-enabled app, log in as admin, return page ready for testing."""
    page.goto(app_server_auth)
    page.locator("#a-user").fill(ADMIN_USER)
    page.locator("#a-pass").fill(ADMIN_PASS)
    page.locator("#auth-btn").click()
    page.wait_for_selector("#input", timeout=10_000)
    return page
