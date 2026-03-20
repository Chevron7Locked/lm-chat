"""Server lifecycle tests: startup, shutdown, PID file management."""

import os, signal, socket, subprocess, sys, time

import pytest

from conftest import _free_port


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SERVER_PY = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server.py")


def _start_server(tmp_path, port, mock_url):
    """Start a server.py subprocess and return the Popen handle."""
    db_path = str(tmp_path / "test.db")
    env = {
        **os.environ,
        "PORT":           str(port),
        "LMSTUDIO_URL":   mock_url,
        "LM_CHAT_AUTH":   "false",
        "LM_CHAT_DB":     db_path,
        "LM_CHAT_LOGS":   str(tmp_path / "logs"),
        "PYTHONPATH": (
            str(os.path.dirname(__file__))
            + os.pathsep + str(os.path.dirname(os.path.dirname(__file__)))
            + os.pathsep + os.environ.get("PYTHONPATH", "")
        ),
    }
    proc = subprocess.Popen(
        [sys.executable, _SERVER_PY],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc, db_path


def _wait_healthy(port, timeout=25.0):
    """Wait until the server responds to /api/health."""
    import urllib.request
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                if r.status in (200, 503):
                    return
        except Exception:
            pass
        time.sleep(0.15)
    raise RuntimeError(f"Server on port {port} did not become healthy within {timeout}s")


def _pid_file_path(db_path, port):
    """Mirror the server's _pid_file() logic."""
    return os.path.join(os.path.dirname(db_path) or ".", f".lm_chat_{port}.pid")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestServerLifecycle:
    def test_server_shuts_down_cleanly_on_sigterm(self, mock_lmstudio, tmp_path):
        port = _free_port()
        time.sleep(0.05)
        proc, _ = _start_server(tmp_path, port, mock_lmstudio.url)
        try:
            _wait_healthy(port)
            proc.send_signal(signal.SIGTERM)
            exit_code = proc.wait(timeout=5)
            assert exit_code == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)

    def test_pid_file_created_on_startup(self, mock_lmstudio, tmp_path):
        port = _free_port()
        time.sleep(0.05)
        proc, db_path = _start_server(tmp_path, port, mock_lmstudio.url)
        try:
            _wait_healthy(port)
            pidfile = _pid_file_path(db_path, port)
            assert os.path.exists(pidfile), f"PID file not found at {pidfile}"
            with open(pidfile) as f:
                pid_in_file = int(f.read().strip())
            assert pid_in_file == proc.pid
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)

    def test_pid_file_removed_on_shutdown(self, mock_lmstudio, tmp_path):
        port = _free_port()
        time.sleep(0.05)
        proc, db_path = _start_server(tmp_path, port, mock_lmstudio.url)
        try:
            _wait_healthy(port)
            pidfile = _pid_file_path(db_path, port)
            assert os.path.exists(pidfile), "PID file should exist while server is running"
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)
            # Give filesystem a moment to sync
            time.sleep(0.1)
            assert not os.path.exists(pidfile), "PID file should be removed after shutdown"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)

    def test_pid_file_is_port_scoped(self, mock_lmstudio, tmp_path):
        port = _free_port()
        time.sleep(0.05)
        proc, db_path = _start_server(tmp_path, port, mock_lmstudio.url)
        try:
            _wait_healthy(port)
            pidfile = _pid_file_path(db_path, port)
            assert os.path.exists(pidfile)
            # Verify the filename contains the port number
            assert str(port) in os.path.basename(pidfile)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
