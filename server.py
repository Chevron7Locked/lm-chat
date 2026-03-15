"""
lm-chat — lightweight web UI for LM Studio with MCP tool integration.
Serves a PWA-ready single-page app, proxies to LM Studio, persists chats in SQLite.
"""

import base64, gzip, hashlib, hmac, html as html_mod, json, logging, math, os, re, secrets, signal, sqlite3, struct, sys, threading, time, uuid, urllib.request, urllib.error
import http.cookies
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from logging.handlers import RotatingFileHandler
from qr import generate_qr_svg

VERSION = "0.1.5"
LMSTUDIO = os.environ.get("LMSTUDIO_URL", "http://localhost:1234")
LMSTUDIO_TOKEN = os.environ.get("LMSTUDIO_TOKEN", "")
PORT = int(os.environ.get("PORT", "3001"))
DB_PATH = os.environ.get("LM_CHAT_DB", os.path.join(os.path.dirname(__file__), "chats.db"))
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
AUTH_ENABLED = os.environ.get("LM_CHAT_AUTH", "true").lower() not in ("0", "false", "no")
CSP = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; worker-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'"
def _get_or_create_signing_key():
    """Get signing key from env, or persist a generated one to .lm_chat_secret."""
    env_key = os.environ.get("LM_CHAT_SECRET")
    if env_key:
        return env_key
    secret_file = os.path.join(os.path.dirname(DB_PATH) or os.path.dirname(os.path.abspath(__file__)), ".lm_chat_secret")
    try:
        with open(secret_file) as f:
            return f.read().strip()
    except FileNotFoundError:
        key = secrets.token_hex(32)
        try:
            fd = os.open(secret_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, key.encode())
            os.close(fd)
        except FileExistsError:
            with open(secret_file) as f:
                return f.read().strip()
        return key

TOTP_SIGNING_KEY = _get_or_create_signing_key()
MAX_BODY_SIZE = 50 * 1024 * 1024  # 50 MB (base64 images can be large)

SESSION_EXPIRY = 30 * 86400        # 30 days
RATE_LIMIT_WINDOW = 900            # 15 minutes
RATE_LIMIT_MAX_ATTEMPTS = 5
PARTIAL_TOKEN_EXPIRY = 300         # 5 minutes
TOTP_SETUP_EXPIRY = 600            # 10 minutes
COMPACT_MIN_TURNS = 10
COMPACT_MAX_CHARS = 6000
SEARCH_MAX_RESULTS = 5000
TITLE_MAX_LENGTH = 500
MODEL_MAX_LENGTH = 200

DEFAULT_INTEGRATIONS = [
    "mcp/brave-search",
    "mcp/memory",
    "mcp/sequential-thinking",
    "mcp/context7",
    "mcp/paper-search",
]

# --- Structured Logging with Rotation ---

LOG_DIR = os.environ.get("LM_CHAT_LOGS", os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"))
LOG_MAX_BYTES = 5 * 1024 * 1024   # 5 MB per file
LOG_BACKUP_COUNT = 5               # keep 5 rotated files (25 MB total max)

def _setup_logger():
    """Configure rotating file logger + optional console output."""
    os.makedirs(LOG_DIR, exist_ok=True)
    logger = logging.getLogger("lm-chat")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    # Rotating file handler — always active, captures everything
    fh = RotatingFileHandler(
        os.path.join(LOG_DIR, "lm-chat.log"),
        maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    fh.setLevel(logging.DEBUG)
    logger.addHandler(fh)
    # Console handler — only INFO+ unless debug mode is on
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    ch.setLevel(logging.DEBUG if os.environ.get("LM_CHAT_DEBUG") else logging.INFO)
    ch.name = "console"
    logger.addHandler(ch)
    return logger

log = _setup_logger()

def set_debug_mode(enabled):
    """Toggle debug verbosity on the console handler at runtime."""
    for h in log.handlers:
        if getattr(h, "name", None) == "console":
            h.setLevel(logging.DEBUG if enabled else logging.INFO)
    log.info(f"Debug mode: {'ON' if enabled else 'OFF'}")


def init_db():
    """Run once at startup: create tables and run migrations."""
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA cache_size=-64000")
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute("PRAGMA journal_size_limit=67108864")
    db.execute("""CREATE TABLE IF NOT EXISTS chats (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL DEFAULT 'New chat',
        model TEXT,
        response_id TEXT,
        updated_at REAL NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
        role TEXT NOT NULL,
        content TEXT,
        name TEXT,
        args TEXT,
        output TEXT,
        created_at REAL NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        salt TEXT NOT NULL,
        display_name TEXT,
        is_admin INTEGER DEFAULT 0,
        created_at REAL NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL
    )""")
    # Schema migrations — add columns that may not exist yet
    _MIGRATIONS = [
        ("chats", "user_id", "TEXT"),
        ("chats", "summary", "TEXT"),
        ("chats", "summary_up_to", "INTEGER"),
        ("users", "totp_secret", "TEXT"),
        ("users", "totp_enabled", "INTEGER DEFAULT 0"),
        ("users", "last_totp_counter", "INTEGER DEFAULT 0"),
        ("messages", "token_count", "INTEGER"),
        ("chats", "pinned", "INTEGER DEFAULT 0"),
        ("chats", "folder", "TEXT DEFAULT ''"),
    ]
    for table, col, typedef in _MIGRATIONS:
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
        except sqlite3.OperationalError:
            pass
    db.execute("""CREATE TABLE IF NOT EXISTS embeddings (
        message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
        vector BLOB NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS user_settings (
        user_id TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT,
        PRIMARY KEY(user_id, key)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS rate_limits (
        ip TEXT PRIMARY KEY,
        attempts INTEGER DEFAULT 0,
        first_attempt REAL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS user_insights (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        content TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'context',
        origin_chat_id TEXT,
        weight REAL DEFAULT 1.0,
        created_at REAL NOT NULL,
        last_used REAL NOT NULL,
        use_count INTEGER DEFAULT 0,
        state TEXT DEFAULT 'active',
        replaced_by TEXT,
        FOREIGN KEY (origin_chat_id) REFERENCES chats(id) ON DELETE SET NULL
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_insights_user_state
        ON user_insights(user_id, state)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_insights_user_cat
        ON user_insights(user_id, category)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_role ON messages(chat_id, role)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_chats_user_id ON chats(user_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_embeddings_message_id ON embeddings(message_id)")
    db.execute("""CREATE TABLE IF NOT EXISTS shared_chats (
        share_id TEXT PRIMARY KEY,
        chat_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        title TEXT NOT NULL,
        messages TEXT NOT NULL,
        created_at REAL NOT NULL
    )""")
    # Ensure "default" user exists for auth-disabled mode (FK constraints require it)
    db.execute(
        """INSERT OR IGNORE INTO users (id, username, password_hash, salt, display_name, is_admin, created_at)
           VALUES ('default', 'default', '', '', 'User', 1, ?)""",
        (time.time(),),
    )
    db.commit()
    db.close()


_thread_local = threading.local()

def get_db():
    """Get a database connection, cached per thread. Call init_db() first at startup."""
    db = getattr(_thread_local, 'db', None)
    if db is not None:
        try:
            db.execute("SELECT 1")
            return db
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            try:
                db.close()
            except Exception:
                pass
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA cache_size=-64000")
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute("PRAGMA foreign_keys=ON")
    db.create_function("ln", 1, math.log)
    _thread_local.db = db
    return db


# --- Password helpers ---

def hash_password(password):
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=64)
    return h.hex(), salt.hex()

def verify_password(password, hash_hex, salt_hex):
    salt = bytes.fromhex(salt_hex)
    h = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=64)
    return hmac.compare_digest(h.hex(), hash_hex)


# --- Session helpers ---

def _hash_token(token):
    """Hash session token for storage (SHA-256). Token has 256 bits of entropy so fast hash is safe."""
    return hashlib.sha256(token.encode()).hexdigest()

def create_session(db, user_id, commit=True):
    token = secrets.token_hex(32)
    token_hash = _hash_token(token)
    expires = time.time() + SESSION_EXPIRY
    db.execute("INSERT INTO sessions (token,user_id,created_at,expires_at) VALUES (?,?,?,?)",
               (token_hash, user_id, time.time(), expires))
    if commit:
        db.commit()
    return token  # return plaintext to client; only hash is stored

def get_session_user(db, token):
    token_hash = _hash_token(token)
    row = db.execute(
        "SELECT s.user_id,s.expires_at,u.id,u.username,u.display_name,u.is_admin,u.totp_enabled "
        "FROM sessions s JOIN users u ON s.user_id=u.id WHERE s.token=?", (token_hash,)
    ).fetchone()
    if not row or row[1] < time.time():
        if row and row[1] < time.time():
            db.execute("DELETE FROM sessions WHERE token=?", (token_hash,))
            db.commit()
        return None
    # Sliding window: only extend if past 50% of session lifetime
    remaining = row[1] - time.time()
    if remaining < SESSION_EXPIRY * 0.5:
        db.execute("UPDATE sessions SET expires_at=? WHERE token=?",
                   (time.time() + SESSION_EXPIRY, token_hash))
        db.commit()
    return {"id": row[2], "username": row[3], "display_name": row[4], "is_admin": row[5], "totp_enabled": row[6] or 0}


# --- Rate limiting (SQLite-backed) ---

def check_rate_limit(ip):
    """Check rate limit AND record a failed attempt atomically. Returns True if allowed."""
    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        now = time.time()
        row = db.execute("SELECT attempts, first_attempt FROM rate_limits WHERE ip=?", (ip,)).fetchone()
        if not row:
            db.execute("INSERT INTO rate_limits (ip, attempts, first_attempt) VALUES (?, 1, ?)", (ip, now))
            db.execute("COMMIT")
            return True
        count, first = row
        if now - first > RATE_LIMIT_WINDOW:
            db.execute("UPDATE rate_limits SET attempts=1, first_attempt=? WHERE ip=?", (now, ip))
            db.execute("COMMIT")
            return True
        if count >= RATE_LIMIT_MAX_ATTEMPTS:
            db.execute("COMMIT")
            return False
        db.execute("UPDATE rate_limits SET attempts=attempts+1 WHERE ip=?", (ip,))
        db.execute("COMMIT")
        return True
    except Exception:
        try:
            db.execute("ROLLBACK")
        except Exception:
            pass
        raise

def clear_rate_limit(ip):
    db = get_db()
    db.execute("DELETE FROM rate_limits WHERE ip=?", (ip,))
    db.commit()

def cleanup_expired_sessions(db):
    now = time.time()
    db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    db.execute("DELETE FROM rate_limits WHERE first_attempt < ?", (now - RATE_LIMIT_WINDOW,))
    db.commit()

# --- In-memory API rate limiter (token bucket) ---

class TokenBucketLimiter:
    """Per-key rate limiter using token bucket algorithm."""
    def __init__(self, rate, capacity):
        self.rate = rate          # tokens per second
        self.capacity = capacity
        self._buckets = {}
        self._lock = threading.Lock()

    def allow(self, key):
        now = time.monotonic()
        with self._lock:
            if key not in self._buckets:
                self._buckets[key] = [self.capacity - 1, now]
                return True
            tokens, last = self._buckets[key]
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens < 1:
                self._buckets[key] = [tokens, now]
                return False
            self._buckets[key] = [tokens - 1, now]
            return True

    def cleanup(self):
        now = time.monotonic()
        with self._lock:
            stale = [k for k, (_, ts) in self._buckets.items() if now - ts > 600]
            for k in stale:
                del self._buckets[k]

_api_limiter = TokenBucketLimiter(rate=2, capacity=60)     # 60 burst, 2/sec sustained
_stream_limiter = TokenBucketLimiter(rate=0.5, capacity=5)  # 5 burst, 1 per 2sec

# Periodic cleanup: run at most once per hour
_last_cleanup = 0.0
_cleanup_lock = threading.Lock()
CLEANUP_INTERVAL = 3600  # 1 hour

def maybe_cleanup_sessions():
    """Run session cleanup at most once per hour (thread-safe)."""
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < CLEANUP_INTERVAL:
        return
    with _cleanup_lock:
        if now - _last_cleanup < CLEANUP_INTERVAL:
            return
        _last_cleanup = now
    try:
        db = get_db()
        cleanup_expired_sessions(db)
        _api_limiter.cleanup()
        _stream_limiter.cleanup()
    except Exception:
        pass

# --- Server-side TOTP setup storage (C3: secret never in client token) ---

_pending_totp = {}
_pending_totp_lock = threading.Lock()

def store_totp_setup(user_id, secret):
    """Store TOTP secret server-side, return opaque setup token."""
    setup_id = secrets.token_urlsafe(32)
    now = time.time()
    with _pending_totp_lock:
        # Cleanup expired entries
        expired = [k for k, v in _pending_totp.items() if now - v["ts"] > TOTP_SETUP_EXPIRY]
        for k in expired:
            del _pending_totp[k]
        _pending_totp[setup_id] = {"user_id": user_id, "secret": secret, "ts": now}
    return setup_id

def get_totp_setup(setup_id, user_id):
    """Retrieve and validate a pending TOTP setup. Returns secret or None."""
    with _pending_totp_lock:
        pending = _pending_totp.get(setup_id)
    if not pending or pending["user_id"] != user_id:
        return None
    if time.time() - pending["ts"] > TOTP_SETUP_EXPIRY:
        with _pending_totp_lock:
            _pending_totp.pop(setup_id, None)
        return None
    return pending["secret"]

def consume_totp_setup(setup_id):
    """Remove a used TOTP setup token."""
    with _pending_totp_lock:
        _pending_totp.pop(setup_id, None)

# --- Validation ---

USERNAME_RE = re.compile(r'^[a-zA-Z0-9_]{3,32}$')

def validate_username(username):
    return bool(USERNAME_RE.match(username or ""))

def validate_password(password):
    return isinstance(password, str) and len(password) >= 8


# --- TOTP helpers (RFC 6238) ---

def generate_totp_secret():
    """Generate 20-byte random secret, return as base32 (no padding)."""
    return base64.b32encode(os.urandom(20)).decode().rstrip('=')

def verify_totp(secret, code, window=1):
    """Verify 6-digit TOTP code. Returns the counter value on success, None on failure."""
    padded = secret + '=' * (-len(secret) % 8)
    key = base64.b32decode(padded)
    now = int(time.time()) // 30
    for offset in range(-window, window + 1):
        counter = now + offset
        msg = struct.pack('>Q', counter)
        h = hmac.new(key, msg, hashlib.sha1).digest()
        o = h[-1] & 0x0F
        truncated = struct.unpack('>I', h[o:o+4])[0] & 0x7FFFFFFF
        otp = str(truncated % 1000000).zfill(6)
        if hmac.compare_digest(otp, code):
            return counter
    return None

def make_totp_uri(username, secret):
    """Build otpauth:// URI for QR code."""
    return f"otpauth://totp/lm-chat:{username}?secret={secret}&issuer=lm-chat"

def sign_partial_token(user_id):
    """Create HMAC-signed partial token for 2FA login (5 min expiry). Base64-encoded to avoid exposing user_id."""
    ts = str(int(time.time()))
    msg = f"{user_id}:{ts}"
    sig = hmac.new(TOTP_SIGNING_KEY.encode(), msg.encode(), hashlib.sha256).hexdigest()[:16]
    return base64.urlsafe_b64encode(f"{user_id}:{ts}:{sig}".encode()).decode()

def verify_partial_token(token_str):
    """Verify and extract user_id from partial token. Returns user_id or None."""
    try:
        decoded = base64.urlsafe_b64decode(token_str.encode()).decode()
    except Exception:
        return None
    parts = decoded.split(':')
    if len(parts) != 3:
        return None
    user_id, ts, sig = parts
    try:
        if time.time() - int(ts) > PARTIAL_TOKEN_EXPIRY:
            return None
    except ValueError:
        return None
    expected = hmac.new(TOTP_SIGNING_KEY.encode(), f"{user_id}:{ts}".encode(), hashlib.sha256).hexdigest()[:16]
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        int(user_id, 16)  # Validate it's a valid hex string (UUID)
        return user_id
    except ValueError:
        return None

# --- Context management ---

def get_embedding(text, token=None):
    """Get embedding vector from LM Studio. Returns list of floats or None."""
    payload = {"model": "_any_", "input": text[:2000]}
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(
            f"{LMSTUDIO}/v1/embeddings",
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return data["data"][0]["embedding"]
    except Exception:
        return None


class PooledHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a bounded thread pool instead of unbounded threads."""
    _pool = ThreadPoolExecutor(max_workers=64)
    daemon_threads = True
    def process_request(self, request, client_address):
        self._pool.submit(self.process_request_thread, request, client_address)
    def server_close(self):
        self._pool.shutdown(wait=True)
        super().server_close()

class Handler(BaseHTTPRequestHandler):
    timeout = 30  # seconds — prevents slow-loris and thread leaks

    # --- Auth middleware ---

    def _parse_session_cookie(self):
        cookie_str = self.headers.get("Cookie", "")
        c = http.cookies.SimpleCookie()
        try:
            c.load(cookie_str)
        except Exception:
            return None
        # Check __Host- prefixed cookie first (HTTPS), fall back to unprefixed (HTTP)
        morsel = c.get("__Host-lm_session") or c.get("lm_session")
        return morsel.value if morsel else None

    def _get_user(self):
        """Returns user dict or None. Does NOT send 401."""
        if not AUTH_ENABLED:
            return {"id": "default", "username": "default", "display_name": "User", "is_admin": 1}
        token = self._parse_session_cookie()
        if not token:
            return None
        db = get_db()
        user = get_session_user(db, token)
        return user

    def _check_csrf(self):
        """Check CSRF header on mutating requests. Returns True if OK, sends 403 and returns False if not."""
        if self.command in ("POST", "DELETE") and self.headers.get("X-Requested-With") != "lm-chat":
            self._error(403, "missing CSRF header")
            return False
        return True

    def _require_auth(self, rate_limit=True):
        """Returns user dict or sends 401/429 and returns None."""
        if not self._check_csrf():
            return None
        # Periodic session cleanup
        maybe_cleanup_sessions()
        user = self._get_user()
        if not user:
            self._error(401, "unauthorized")
            return None
        # API rate limiting (per-user)
        if rate_limit and not _api_limiter.allow(f"api:{user['id']}"):
            self._error(429, "too many requests")
            return None
        return user

    def _verify_chat_owner(self, db, chat_id, user_id):
        """Returns True if chat belongs to user (or auth disabled). Sends 404 if not. Caller must close db."""
        if not AUTH_ENABLED:
            return True
        row = db.execute("SELECT user_id FROM chats WHERE id=?", (chat_id,)).fetchone()
        if not row or row[0] != user_id:
            self._error(404, "chat not found")
            return False
        return True

    def _secure_flag(self):
        """Return '; Secure' if running behind HTTPS, else empty string."""
        if os.environ.get("LM_CHAT_HTTPS") or self.headers.get("X-Forwarded-Proto") == "https":
            return "; Secure"
        return ""

    def _set_session_cookie(self, token):
        secure = self._secure_flag()
        if secure:
            cookie = f"__Host-lm_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_EXPIRY}; Secure"
        else:
            cookie = f"lm_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_EXPIRY}"
        self.send_header("Set-Cookie", cookie)

    def _clear_session_cookie(self):
        secure = self._secure_flag()
        if secure:
            self.send_header("Set-Cookie", "__Host-lm_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0; Secure")
        else:
            self.send_header("Set-Cookie", "lm_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")

    def _send_security_headers(self, csp=None, referrer="strict-origin-when-cross-origin"):
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", csp or CSP)
        self.send_header("Referrer-Policy", referrer)

    def _json_response_with_cookie(self, code, data, cookie_token=None, clear_cookie=False):
        """Like _json_response but can set/clear cookie before end_headers."""
        if isinstance(data, (dict, list)):
            data = json.dumps(data).encode()
        elif isinstance(data, str):
            data = data.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
        self._send_security_headers()
        if cookie_token:
            self._set_session_cookie(cookie_token)
        if clear_cookie:
            self._clear_session_cookie()
        self.end_headers()
        self.wfile.write(data)

    # --- Routing ---

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._serve_file("index.html", "text/html")
        elif self.path == "/manifest.json":
            self._serve_file("manifest.json", "application/json")
        elif self.path == "/sw.js":
            self._serve_file("sw.js", "application/javascript")
        elif self.path == "/lm-chat-logo.svg":
            self._serve_file("lm-chat-logo.svg", "image/svg+xml")
        elif self.path == "/style.css":
            self._serve_file("style.css", "text/css")
        elif self.path == "/app.js":
            self._serve_file("app.js", "application/javascript")
        elif self.path == "/api/health":
            status = {"ok": True, "version": VERSION, "db": False, "lmstudio": False}
            try:
                db = get_db()
                db.execute("SELECT 1")
                status["db"] = True
            except Exception as e:
                log.warning(f"Health check DB failed: {e}")
            try:
                req = urllib.request.Request(f"{LMSTUDIO}/api/v1/models", method="GET")
                # Try env var token first, then any stored user key
                token = LMSTUDIO_TOKEN
                if not token:
                    try:
                        db2 = get_db()
                        row = db2.execute("SELECT value FROM user_settings WHERE key='lm_apikey' AND value != '' LIMIT 1").fetchone()
                        if row:
                            token = row[0]
                    except Exception as e:
                        log.warning(f"Health check: cannot read API key from DB: {e}")
                if token:
                    req.add_header("Authorization", f"Bearer {token}")
                with urllib.request.urlopen(req, timeout=3) as resp:
                    resp.read()
                status["lmstudio"] = True
            except Exception:
                pass
            status["ok"] = status["db"] and status["lmstudio"]
            code = 200 if status["ok"] else 503
            self._json_response(code, status)
        elif self.path.startswith("/share/"):
            share_id = self.path.split("/")[2] if len(self.path.split("/")) > 2 else ""
            self._serve_shared(share_id)
        elif self.path == "/api/debug":
            user = self._require_auth()
            if not user:
                return
            if not user.get("is_admin"):
                return self._error(403, "admin only")
            # Return current debug state + log file info
            console_level = "DEBUG"
            for h in log.handlers:
                if getattr(h, "name", None) == "console":
                    console_level = logging.getLevelName(h.level)
            log_files = []
            if os.path.isdir(LOG_DIR):
                for f in sorted(os.listdir(LOG_DIR)):
                    fp = os.path.join(LOG_DIR, f)
                    if os.path.isfile(fp):
                        log_files.append({"name": f, "size": os.path.getsize(fp)})
            self._json_response(200, {
                "enabled": console_level == "DEBUG",
                "log_dir": LOG_DIR,
                "log_files": log_files,
                "max_bytes": LOG_MAX_BYTES,
                "backup_count": LOG_BACKUP_COUNT,
            })
        elif self.path == "/api/auth/me":
            self._auth_me()
        elif self.path == "/api/auth/users":
            self._auth_list_users()
        elif self.path == "/api/auth/settings":
            self._get_settings()
        elif self.path == "/api/models":
            u = self._get_user()
            self._proxy_get("/api/v1/models", u["id"] if u else None)
        elif self.path == "/api/chats":
            self._list_chats()
        elif self.path.startswith("/api/chats/") and "/messages" in self.path:
            chat_id = self.path.split("/")[3]
            self._get_messages(chat_id)
        elif self.path == "/api/insights":
            self._list_insights()
        elif self.path == "/api/insights/settings":
            self._get_insight_settings()
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
        self.send_header("Access-Control-Max-Age", "86400")
        self._send_security_headers()
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_BODY_SIZE:
                return self._error(413, "request too large")
            body = json.loads(self.rfile.read(length)) if length else {}
        except (ValueError, json.JSONDecodeError):
            return self._error(400, "invalid request body")

        if self.path == "/api/auth/setup":
            self._auth_setup(body)
        elif self.path == "/api/auth/login":
            self._auth_login(body)
        elif self.path == "/api/auth/logout":
            self._auth_logout()
        elif self.path == "/api/auth/invite":
            self._auth_invite(body)
        elif self.path == "/api/auth/change-password":
            self._auth_change_password(body)
        elif self.path == "/api/auth/profile":
            self._auth_update_profile(body)
        elif self.path == "/api/auth/totp/setup":
            self._totp_setup()
        elif self.path == "/api/auth/totp/verify":
            self._totp_verify(body)
        elif self.path == "/api/auth/totp/disable":
            self._totp_disable(body)
        elif self.path == "/api/auth/totp/login":
            self._totp_login(body)
        elif self.path == "/api/auth/settings":
            self._save_settings(body)
        elif self.path == "/api/debug":
            user = self._require_auth()
            if not user:
                return
            if not user.get("is_admin"):
                return self._error(403, "admin only")
            enabled = body.get("enabled", True)
            set_debug_mode(enabled)
            self._json_response(200, {"enabled": enabled})
        elif self.path == "/api/chat":
            self._handle_chat(body)
        elif self.path == "/api/chat/stream":
            self._handle_chat_stream(body)
        elif self.path == "/api/chats":
            self._create_chat(body)
        elif self.path.startswith("/api/chats/") and self.path.endswith("/title"):
            chat_id = self.path.split("/")[3]
            self._update_title(chat_id, body)
        elif self.path.startswith("/api/chats/") and self.path.endswith("/messages/truncate"):
            chat_id = self.path.split("/")[3]
            self._truncate_messages(chat_id, body)
        elif self.path.startswith("/api/chats/") and self.path.endswith("/fork"):
            chat_id = self.path.split("/")[3]
            self._fork_chat(chat_id, body)
        elif self.path.startswith("/api/chats/") and self.path.endswith("/compact"):
            chat_id = self.path.split("/")[3]
            self._compact_chat(chat_id, body)
        elif self.path.startswith("/api/chats/") and self.path.endswith("/pin"):
            chat_id = self.path.split("/")[3]
            self._toggle_pin(chat_id)
        elif self.path.startswith("/api/chats/") and self.path.endswith("/folder"):
            chat_id = self.path.split("/")[3]
            self._set_folder(chat_id, body)
        elif self.path == "/api/search":
            self._search_messages(body)
        elif self.path == "/api/insights/distill":
            self._distill_insights(body)
        elif self.path == "/api/insights/refine":
            self._refine_insights(body)
        elif self.path == "/api/insights":
            self._add_insight(body)
        elif self.path.startswith("/api/insights/") and self.path.endswith("/edit"):
            insight_id = self.path.split("/")[3]
            self._edit_insight(insight_id, body)
        elif self.path == "/api/insights/settings":
            self._save_insight_settings(body)
        elif self.path.startswith("/api/chats/") and self.path.endswith("/share"):
            chat_id = self.path.split("/")[3]
            self._share_chat(chat_id)
        else:
            self.send_error(404)

    def do_DELETE(self):
        if self.path.startswith("/api/auth/users/"):
            user_id = self.path.split("/")[4]
            self._auth_delete_user(user_id)
        elif self.path.startswith("/api/chats/"):
            parts = self.path.split("/")
            chat_id = parts[3]
            if len(parts) >= 6 and parts[4] == "messages" and parts[5] == "last":
                self._delete_last_response(chat_id)
            elif len(parts) == 5 and parts[4] == "share":
                self._unshare_chat(chat_id)
            elif len(parts) == 4:
                self._delete_chat(chat_id)
            else:
                self.send_error(404)
        elif self.path == "/api/insights":
            self._clear_insights()
        elif self.path.startswith("/api/insights/"):
            insight_id = self.path.split("/")[3]
            self._delete_insight(insight_id)
        else:
            self.send_error(404)

    # --- Auth endpoints ---

    def _auth_me(self):
        if not AUTH_ENABLED:
            return self._json_response(200, {"auth_enabled": False})
        db = get_db()
        user_count = db.execute("SELECT COUNT(*) FROM users WHERE username != 'default'").fetchone()[0]
        user = self._get_user()
        if not user:
            return self._json_response(200, {
                "auth_enabled": True, "user": None, "needs_setup": user_count == 0
            })
        return self._json_response(200, {
            "auth_enabled": True, "user": user, "needs_setup": False
        })

    def _auth_setup(self, body):
        if not self._check_csrf():
            return
        if not AUTH_ENABLED:
            return self._error(400, "auth not enabled")
        username = (body.get("username") or "").strip().lower()
        password = body.get("password") or ""
        display_name = (body.get("display_name") or "").strip() or username
        if not validate_username(username):
            return self._error(400, "username must be 3-32 chars, alphanumeric + underscore")
        if not validate_password(password):
            return self._error(400, "password must be at least 8 characters")
        user_id = uuid.uuid4().hex
        pw_hash, salt = hash_password(password)
        db = get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) FROM users WHERE username != 'default'").fetchone()[0]
            if count > 0:
                db.rollback()
                return self._error(400, "setup already complete")
            db.execute(
                "INSERT INTO users (id,username,password_hash,salt,display_name,is_admin,created_at) VALUES (?,?,?,?,?,?,?)",
                (user_id, username, pw_hash, salt, display_name, 1, time.time()),
            )
            db.execute("UPDATE chats SET user_id=? WHERE user_id IS NULL", (user_id,))
            token = create_session(db, user_id, commit=False)
            db.commit()
        except Exception:
            db.rollback()
            return self._error(500, "setup failed")
        user = {"id": user_id, "username": username, "display_name": display_name, "is_admin": 1}
        self._json_response_with_cookie(200, {"user": user}, cookie_token=token)

    def _auth_login(self, body):
        if not self._check_csrf():
            return
        if not AUTH_ENABLED:
            return self._error(400, "auth not enabled")
        ip = self.client_address[0]
        if not check_rate_limit(ip):
            return self._error(429, "too many login attempts, try again in 15 minutes")
        username = (body.get("username") or "").strip().lower()
        password = body.get("password") or ""
        db = get_db()
        cleanup_expired_sessions(db)
        row = db.execute(
            "SELECT id,username,password_hash,salt,display_name,is_admin FROM users WHERE username=?",
            (username,),
        ).fetchone()
        if not row or not verify_password(password, row[2], row[3]):

            return self._error(401, "invalid username or password")
        # Check if 2FA is enabled
        totp_row = db.execute("SELECT totp_enabled FROM users WHERE id=?", (row[0],)).fetchone()
        if totp_row and totp_row[0]:
            partial = sign_partial_token(row[0])
            return self._json_response(200, {"needs_totp": True, "partial_token": partial})
        # Only clear rate limit after FULL authentication (no 2FA pending)
        clear_rate_limit(ip)
        token = create_session(db, row[0])
        user = {"id": row[0], "username": row[1], "display_name": row[4], "is_admin": row[5]}
        self._json_response_with_cookie(200, {"user": user}, cookie_token=token)

    def _auth_logout(self):
        if not self._check_csrf():
            return
        token = self._parse_session_cookie()
        if token:
            db = get_db()
            db.execute("DELETE FROM sessions WHERE token=?", (_hash_token(token),))
            db.commit()
        self._json_response_with_cookie(200, {"ok": True}, clear_cookie=True)

    def _auth_invite(self, body):
        user = self._require_auth()
        if not user:
            return
        if not user.get("is_admin"):
            return self._error(403, "admin required")
        username = (body.get("username") or "").strip().lower()
        password = body.get("password") or ""
        display_name = (body.get("display_name") or "").strip() or username
        if not validate_username(username):
            return self._error(400, "username must be 3-32 chars, alphanumeric + underscore")
        if not validate_password(password):
            return self._error(400, "password must be at least 8 characters")
        db = get_db()
        user_id = uuid.uuid4().hex
        pw_hash, salt = hash_password(password)
        try:
            db.execute(
                "INSERT INTO users (id,username,password_hash,salt,display_name,is_admin,created_at) VALUES (?,?,?,?,?,?,?)",
                (user_id, username, pw_hash, salt, display_name, 0, time.time()),
            )
            db.commit()
        except sqlite3.IntegrityError:
            return self._error(409, "username already taken")
        new_user = {"id": user_id, "username": username, "display_name": display_name, "is_admin": 0}
        self._json_response(200, {"user": new_user})

    def _auth_delete_user(self, target_id):
        user = self._require_auth()
        if not user:
            return
        if not user.get("is_admin"):
            return self._error(403, "admin required")
        if target_id == user["id"]:
            return self._error(400, "cannot delete yourself")
        db = get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            target = db.execute("SELECT id FROM users WHERE id=?", (target_id,)).fetchone()
            if not target:
                db.rollback()
                return self._error(404, "user not found")
            chat_ids = [r[0] for r in db.execute("SELECT id FROM chats WHERE user_id=?", (target_id,)).fetchall()]
            for cid in chat_ids:
                db.execute("DELETE FROM messages WHERE chat_id=?", (cid,))
            db.execute("DELETE FROM chats WHERE user_id=?", (target_id,))
            db.execute("DELETE FROM shared_chats WHERE user_id=?", (target_id,))
            db.execute("DELETE FROM user_settings WHERE user_id=?", (target_id,))
            db.execute("DELETE FROM sessions WHERE user_id=?", (target_id,))
            db.execute("DELETE FROM users WHERE id=?", (target_id,))
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            return self._error(500, "failed to delete user")
        self._json_response(200, {"ok": True})
    def _auth_change_password(self, body):
        user = self._require_auth()
        if not user:
            return
        current_password = body.get("current_password") or ""
        new_password = body.get("new_password") or ""
        if not validate_password(new_password):
            return self._error(400, "new password must be at least 8 characters")
        db = get_db()
        row = db.execute("SELECT password_hash,salt FROM users WHERE id=?", (user["id"],)).fetchone()
        if not row or not verify_password(current_password, row[0], row[1]):
            return self._error(401, "current password is incorrect")
        pw_hash, salt = hash_password(new_password)
        db.execute("UPDATE users SET password_hash=?,salt=? WHERE id=?", (pw_hash, salt, user["id"]))
        # Invalidate all existing sessions
        db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
        # Create fresh session
        token = create_session(db, user["id"])
        self._json_response_with_cookie(200, {"ok": True}, cookie_token=token)

    def _auth_update_profile(self, body):
        user = self._require_auth()
        if not user:
            return
        display_name = (body.get("display_name") or "").strip()
        if not display_name:
            return self._error(400, "display name required")
        if len(display_name) > 100:
            return self._error(400, "display name too long (max 100)")
        db = get_db()
        db.execute("UPDATE users SET display_name=? WHERE id=?", (display_name, user["id"]))
        db.commit()
        self._json_response(200, {"ok": True})

    def _auth_list_users(self):
        user = self._require_auth()
        if not user:
            return
        if not user.get("is_admin"):
            return self._error(403, "admin required")
        db = get_db()
        rows = db.execute("SELECT id,username,display_name,is_admin,created_at FROM users ORDER BY created_at").fetchall()
        users = [{"id": r[0], "username": r[1], "display_name": r[2], "is_admin": r[3], "created_at": r[4]} for r in rows]
        self._json_response(200, users)

    # --- TOTP 2FA ---

    def _totp_setup(self):
        """Generate TOTP secret and return QR SVG. Does NOT enable 2FA yet."""
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        existing = db.execute("SELECT totp_enabled FROM users WHERE id=?", (user["id"],)).fetchone()
        if existing and existing[0]:
            return self._error(400, "2FA already enabled, disable first")
        secret = generate_totp_secret()
        uri = make_totp_uri(user["username"], secret)
        qr_svg = generate_qr_svg(uri) or ""
        # Store secret server-side; client gets opaque token (secret never in transport token)
        setup_token = store_totp_setup(user["id"], secret)
        self._json_response(200, {"secret": secret, "qr_svg": qr_svg, "setup_token": setup_token})

    def _totp_verify(self, body):
        """Verify TOTP code and enable 2FA."""
        user = self._require_auth()
        if not user:
            return
        code = (body.get("code") or "").strip()
        setup_token = body.get("setup_token") or ""
        if not code or len(code) != 6:
            return self._error(400, "6-digit code required")
        # Look up secret from server-side storage (never in the token)
        tok_secret = get_totp_setup(setup_token, user["id"])
        if not tok_secret:
            return self._error(400, "invalid or expired setup, try again")
        # Verify TOTP code against the stored secret
        counter = verify_totp(tok_secret, code)
        if counter is None:
            return self._error(400, "invalid code, try again")
        # Consume the setup token and persist the secret
        consume_totp_setup(setup_token)
        db = get_db()
        db.execute("UPDATE users SET totp_secret=?, totp_enabled=1, last_totp_counter=? WHERE id=?", (tok_secret, counter, user["id"]))
        db.commit()
        self._json_response(200, {"ok": True})

    def _totp_disable(self, body):
        """Disable 2FA after verifying current TOTP code."""
        user = self._require_auth()
        if not user:
            return
        code = (body.get("code") or "").strip()
        db = get_db()
        row = db.execute("SELECT totp_secret,totp_enabled FROM users WHERE id=?", (user["id"],)).fetchone()
        if not row or not row[1]:
            return self._error(400, "2FA not enabled")
        counter = verify_totp(row[0], code)
        if counter is None:
            return self._error(400, "invalid code")
        db.execute("UPDATE users SET totp_enabled=0, totp_secret=NULL WHERE id=?", (user["id"],))
        db.commit()
        self._json_response(200, {"ok": True})

    def _totp_login(self, body):
        """Complete 2FA login with partial token + TOTP code."""
        if not self._check_csrf():
            return
        ip = self.client_address[0]
        if not check_rate_limit(ip):
            return self._error(429, "too many attempts, try again later")
        partial = body.get("partial_token") or ""
        code = (body.get("code") or "").strip()
        user_id = verify_partial_token(partial)
        if not user_id:
            return self._error(401, "expired or invalid token, please log in again")
        if not code or len(code) != 6:
            return self._error(400, "6-digit code required")
        db = get_db()
        row = db.execute("SELECT totp_secret,username,display_name,is_admin,last_totp_counter FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:

            return self._error(401, "invalid or reused code")
        counter = verify_totp(row[0], code)
        if counter is None or counter <= (row[4] or 0):

            return self._error(401, "invalid or reused code")
        db.execute("UPDATE users SET last_totp_counter=? WHERE id=?", (counter, user_id))
        db.commit()
        token = create_session(db, user_id)
        clear_rate_limit(ip)  # Clear rate limit after full 2FA success
        user = {"id": user_id, "username": row[1], "display_name": row[2], "is_admin": row[3]}
        self._json_response_with_cookie(200, {"user": user}, cookie_token=token)

    # --- User Settings API (H4: server-side secrets) ---

    ALLOWED_SETTINGS = {"lm_apikey", "lm_url", "remote_mcps"}

    def _get_settings(self):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        rows = db.execute("SELECT key, value FROM user_settings WHERE user_id=?", (user["id"],)).fetchall()
        result = {}
        for key, value in rows:
            if key == "lm_apikey":
                result[key] = bool(value)
            elif key == "remote_mcps":
                try:
                    mcps = json.loads(value) if value else []
                    result[key] = [{"label": m.get("label", ""), "url": m.get("url", ""),
                                    "on": m.get("on", True), "has_auth": bool(m.get("auth"))}
                                   for m in mcps]
                except (json.JSONDecodeError, TypeError):
                    result[key] = []
            else:
                result[key] = value
        self._json_response(200, result)

    def _save_settings(self, body):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        saved = {}
        for key, value in body.items():
            if key not in self.ALLOWED_SETTINGS:
                continue
            if key == "lm_apikey":
                str_val = str(value).strip() if value else ""
                db.execute(
                    "INSERT OR REPLACE INTO user_settings (user_id, key, value) VALUES (?, ?, ?)",
                    (user["id"], key, str_val),
                )
                saved[key] = bool(str_val)
            elif key == "remote_mcps":
                existing = []
                row = db.execute(
                    "SELECT value FROM user_settings WHERE user_id=? AND key=?",
                    (user["id"], key),
                ).fetchone()
                if row and row[0]:
                    try:
                        existing = json.loads(row[0])
                    except (json.JSONDecodeError, TypeError):
                        existing = []
                existing_auth = {(m.get("label"), m.get("url")): m.get("auth", "")
                                 for m in existing}
                new_mcps = []
                for m in (value if isinstance(value, list) else []):
                    entry = {"label": m.get("label", ""), "url": m.get("url", ""),
                             "on": m.get("on", True)}
                    if "auth" in m:
                        entry["auth"] = m["auth"]
                    else:
                        entry["auth"] = existing_auth.get(
                            (entry["label"], entry["url"]), "")
                    new_mcps.append(entry)
                db.execute(
                    "INSERT OR REPLACE INTO user_settings (user_id, key, value) VALUES (?, ?, ?)",
                    (user["id"], key, json.dumps(new_mcps)),
                )
                saved[key] = [{"label": m["label"], "url": m["url"],
                               "on": m["on"], "has_auth": bool(m.get("auth"))}
                              for m in new_mcps]
            else:
                str_val = str(value).strip() if value else ""
                db.execute(
                    "INSERT OR REPLACE INTO user_settings (user_id, key, value) VALUES (?, ?, ?)",
                    (user["id"], key, str_val),
                )
                saved[key] = str_val
        db.commit()
        self._json_response(200, saved)
    def _get_user_lm_apikey(self, user_id):
        """Get stored LM Studio API key for a user."""
        db = get_db()
        row = db.execute(
            "SELECT value FROM user_settings WHERE user_id=? AND key='lm_apikey'",
            (user_id,),
        ).fetchone()
        return row[0] if row and row[0] else ""

    def _get_user_remote_mcps(self, user_id):
        """Get stored remote MCP configs (with auth) for a user."""
        db = get_db()
        row = db.execute(
            "SELECT value FROM user_settings WHERE user_id=? AND key='remote_mcps'",
            (user_id,),
        ).fetchone()
        if row and row[0]:
            try:
                return json.loads(row[0])
            except (json.JSONDecodeError, TypeError):
                pass
        return []

    def _inject_mcp_auth(self, integrations, user_id):
        """Inject server-side auth headers into ephemeral_mcp integrations."""
        # Short-circuit: skip DB lookup if no ephemeral MCPs in the list
        if not any(isinstance(i, dict) and i.get("type") == "ephemeral_mcp" for i in integrations):
            return integrations
        stored_mcps = self._get_user_remote_mcps(user_id)
        auth_lookup = {(m.get("label"), m.get("url")): m.get("auth", "")
                       for m in stored_mcps}
        result = []
        for item in integrations:
            if isinstance(item, dict) and item.get("type") == "ephemeral_mcp":
                key = (item.get("server_label"), item.get("server_url"))
                auth = auth_lookup.get(key, "")
                new_item = {k: v for k, v in item.items() if k != "headers"}
                if auth:
                    new_item["headers"] = {"Authorization": auth}
                result.append(new_item)
            else:
                result.append(item)
        return result

    # --- Chat API ---

    def _handle_chat(self, body):
        user = self._require_auth()
        if not user:
            return
        if len(body.get("model", "")) > MODEL_MAX_LENGTH:
            return self._error(400, f"model too long (max {MODEL_MAX_LENGTH})")

        body["integrations"] = self._inject_mcp_auth(
            body.get("integrations", DEFAULT_INTEGRATIONS), user["id"])

        payload = self._build_lmstudio_payload(body)

        try:
            data = self._lmstudio_chat(payload, user["id"], timeout=300)

            chat_id = body.get("chat_id")
            is_incognito = body.get("incognito", False)
            if chat_id and not is_incognito:
                db = get_db()
                if not self._verify_chat_owner(db, chat_id, user["id"]):
                    return
                # Extract tool calls from output
                tool_calls = []
                if isinstance(data.get("output"), list):
                    for item in data["output"]:
                        if item.get("type") == "tool_call":
                            tool_calls.append(item)
                content = self._extract_content(data)
                self._persist_chat_messages(
                    db, chat_id, body.get("input", ""), content, tool_calls,
                    data.get("response_id"), data.get("usage") or {},
                )
            self._json_response(200, data)
        except urllib.error.HTTPError as e:
            self._json_response(e.code, e.read())
        except Exception as e:
            log.error(f"chat completion: {e}")
            self._error(502, "upstream service unavailable")

    def _handle_chat_stream(self, body):
        user = self._require_auth()
        if not user:
            return
        # Stream-specific rate limiting (stricter than general API)
        if not _stream_limiter.allow(f"stream:{user['id']}"):
            return self._error(429, "too many requests — please wait")
        if len(body.get("model", "")) > MODEL_MAX_LENGTH:
            return self._error(400, f"model too long (max {MODEL_MAX_LENGTH})")

        # Incognito mode: skip persistence, logging, and memory
        is_incognito = body.get("incognito", False)

        # Verify chat ownership before streaming
        chat_id = body.get("chat_id")
        db = get_db() if chat_id else None
        if chat_id and AUTH_ENABLED:
            if not self._verify_chat_owner(db, chat_id, user["id"]):
                return

        # Context: LM Studio manages history via response_id chaining.
        # Summary injection is redundant (model sees summary AND full history).
        # Manual /compact still works via _compact_chat for explicit user action.
        system_prompt = body.get("system_prompt") or ""

        # Memory: inject user insights into system prompt (skip in incognito)
        if not is_incognito:
            mem_db = None
            try:
                mem_db = get_db()
                mem_enabled = mem_db.execute(
                    "SELECT value FROM user_settings WHERE user_id=? AND key='memory_enabled'",
                    (user["id"],)
                ).fetchone()
                if mem_enabled is None or mem_enabled[0] != "false":
                    max_inject_row = mem_db.execute(
                        "SELECT value FROM user_settings WHERE user_id=? AND key='memory_max_inject'",
                        (user["id"],)
                    ).fetchone()
                    try:
                        max_inject = int(max_inject_row[0]) if max_inject_row and max_inject_row[0] else 30
                    except (ValueError, TypeError):
                        max_inject = 30
                    insights = self._get_top_insights(mem_db, user["id"], limit=max_inject)
                    if insights:
                        memory_block = self._format_insights_for_prompt(insights)
                        # Append memories AFTER system prompt (not before) so the frozen
                        # system prompt prefix stays identical across turns, enabling
                        # llama.cpp / LM Studio KV cache prefix reuse.
                        system_prompt = system_prompt + "\n\n" + memory_block if system_prompt else memory_block
                        self._touch_insights(mem_db, [i["id"] for i in insights])
            except Exception as e:
                log.error(f"memory injection: {e}")
        # Instruction sandwich: append core instruction reminder at end of system prompt.
        # Local LLMs (Qwen, Llama, Mistral) have strong recency bias — instructions at
        # the end of the system prompt get more attention than those in the middle.
        if system_prompt:
            system_prompt += "\n\n## REMINDERS\n- Be direct and concise. Lead with the answer.\n- Use your tools when they add value.\n- If unsure about a fact, say so or look it up."

        body["integrations"] = self._inject_mcp_auth(
            body.get("integrations", DEFAULT_INTEGRATIONS), user["id"])

        payload = self._build_lmstudio_payload(body, system_prompt=system_prompt or None, stream=True)

        # Debug logging (skip content in incognito mode)
        if is_incognito:
            log.debug(f"REQ [incognito] model={payload.get('model')}")
        else:
            log.debug(f"REQ model={payload.get('model')} stream={payload.get('stream')} ctx={payload.get('context_length')} prev_resp_id={payload.get('previous_response_id')}")
            log.debug(f"REQ system_prompt: [{len(payload.get('system_prompt') or '')} chars]")
            inp = payload.get('input', '')
            log.debug(f"REQ input: {(str(inp)[:150]) if isinstance(inp, list) else (inp or '')[:150]}")
            params = {k: payload[k] for k in ('temperature','top_p','top_k','min_p','repeat_penalty','max_output_tokens','reasoning') if k in payload}
            if params:
                log.debug(f"REQ params: {params}")

        headers = {"Content-Type": "application/json"}
        token = self._get_lmstudio_token(user["id"])
        if token:
            headers["Authorization"] = f"Bearer {token}"

        req = urllib.request.Request(
            f"{LMSTUDIO}/api/v1/chat",
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )

        try:
            resp = urllib.request.urlopen(req, timeout=300)
        except urllib.error.HTTPError as e:
            self._json_response(e.code, e.read())
            return
        except Exception as e:
            log.error(f"chat stream: {e}")
            self._error(502, "upstream service unavailable")
            return

        # Send SSE headers
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self._send_security_headers()
        self.end_headers()

        # Collect data for persistence
        content_parts = []
        reasoning_parts = []
        tool_calls = []
        current_tool = None
        response_id = None
        event_type = ""
        stream_usage = {}
        stream_complete = False

        try:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace")
                self.wfile.write(raw_line)
                self.wfile.flush()

                # Parse events for persistence
                stripped = line.strip()
                if stripped.startswith("event:"):
                    event_type = stripped[6:].strip()
                elif stripped.startswith("data:"):
                    data_str = stripped[5:].strip()
                    try:
                        data = json.loads(data_str)
                    except (json.JSONDecodeError, ValueError):
                        data = {}

                    # Log all events to find response_id (skip content in incognito)
                    if event_type != "message.delta" and not is_incognito:
                        log.debug(f"SSE {event_type}")

                    if event_type == "message.delta":
                        content_parts.append(data.get("content") or "")
                    elif event_type == "reasoning.delta":
                        reasoning_parts.append(data.get("content") or "")
                    elif event_type == "tool_call.start":
                        current_tool = {
                            "id": data.get("id", ""),
                            "tool": data.get("tool", ""),
                            "arguments": data.get("arguments", ""),
                            "output": None,
                        }
                    elif event_type == "tool_call.arguments" and current_tool:
                        current_tool["arguments"] += data.get("argumentsDelta", "")
                    elif event_type == "tool_call.success" and current_tool:
                        current_tool["output"] = data.get("output")
                        tool_calls.append(current_tool)
                        current_tool = None
                    elif event_type == "tool_call.failure":
                        if current_tool:
                            current_tool["output"] = data.get("error", "Tool call failed")
                            tool_calls.append(current_tool)
                            current_tool = None
                    elif event_type == "chat.end":
                        result = data.get("result", {})
                        response_id = data.get("response_id") or result.get("response_id")
                        stats = result.get("stats", {})
                        stream_usage = data.get("usage") or {
                            "input_tokens": stats.get("input_tokens", 0),
                            "output_tokens": stats.get("total_output_tokens", 0),
                        }
                        stream_complete = True
                        if not is_incognito:
                            log.debug(f"RESP chat.end resp_id={response_id} usage={stream_usage}")
                            log.debug(f"RESP content: [{len(''.join(content_parts))} chars]")
                            if tool_calls:
                                log.debug(f"RESP tools: {len(tool_calls)} calls")
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected — don't persist partial responses
            log.debug("Stream aborted by client")
        finally:
            resp.close()

        # Only persist complete responses (skip if client disconnected mid-stream)
        chat_id = body.get("chat_id")
        if chat_id and stream_complete and not is_incognito:
            db = get_db()
            reasoning = "".join(reasoning_parts).strip()
            content = ("".join(content_parts))
            if reasoning:
                content = f"<think>{reasoning}</think>{content}"
            self._persist_chat_messages(
                db, chat_id, body.get("input", ""), content, tool_calls,
                response_id, stream_usage,
            )

            # Index embeddings in background thread (truly non-blocking)
            _embed_token = self._get_lmstudio_token(user["id"])
            def _index_embeddings(cid, tok2):
                try:
                    db2 = get_db()
                    for role_to_embed in ("user", "assistant"):
                        rows = db2.execute(
                            "SELECT m.id, m.content FROM messages m LEFT JOIN embeddings e ON m.id = e.message_id WHERE m.chat_id=? AND m.role=? AND e.message_id IS NULL ORDER BY m.id DESC LIMIT 2",
                            (cid, role_to_embed)
                        ).fetchall()
                        for mid, emb_content in rows:
                            if emb_content:
                                vec = get_embedding(emb_content, tok2)
                                if vec:
                                    blob = struct.pack(f'{len(vec)}f', *vec)
                                    db2.execute("INSERT OR IGNORE INTO embeddings (message_id, vector) VALUES (?, ?)", (mid, blob))
                    db2.commit()
                except Exception:
                    log.debug("Background embedding indexing failed", exc_info=True)
            threading.Thread(target=_index_embeddings, args=(chat_id, _embed_token), daemon=True).start()
    def _create_chat(self, body):
        user = self._require_auth()
        if not user:
            return
        title = body.get("title", "New chat")
        model = body.get("model", "")
        if len(title) > TITLE_MAX_LENGTH:
            return self._error(400, f"title too long (max {TITLE_MAX_LENGTH})")
        if len(model) > MODEL_MAX_LENGTH:
            return self._error(400, f"model too long (max {MODEL_MAX_LENGTH})")
        db = get_db()
        chat_id = f"c{uuid.uuid4().hex[:12]}"
        now = time.time()
        db.execute(
            "INSERT INTO chats (id,title,model,updated_at,user_id) VALUES (?,?,?,?,?)",
            (chat_id, title, model, now, user["id"]),
        )
        db.commit()
        self._json_response(200, {"id": chat_id, "title": title})

    def _list_chats(self):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        where, params = self._user_filter(user)
        rows = db.execute(
            f"SELECT id,title,model,response_id,updated_at,pinned,folder FROM chats {where} ORDER BY pinned DESC, updated_at DESC",
            params,
        ).fetchall()
        chats = [{"id": r[0], "title": r[1], "model": r[2], "response_id": r[3], "updated_at": r[4], "pinned": r[5] or 0, "folder": r[6] or ""} for r in rows]
        self._json_response(200, chats)

    def _toggle_pin(self, chat_id):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        if not self._verify_chat_owner(db, chat_id, user["id"]):
            return
        row = db.execute("SELECT pinned FROM chats WHERE id=?", (chat_id,)).fetchone()
        if not row:
            return self._error(404, "chat not found")
        new_val = 0 if row[0] else 1
        db.execute("UPDATE chats SET pinned=? WHERE id=?", (new_val, chat_id))
        db.commit()
        self._json_response(200, {"pinned": new_val})

    def _set_folder(self, chat_id, body):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        if not self._verify_chat_owner(db, chat_id, user["id"]):
            return
        folder = str(body.get("folder", "")).strip()[:50]
        db.execute("UPDATE chats SET folder=? WHERE id=?", (folder, chat_id))
        db.commit()
        self._json_response(200, {"folder": folder})

    def _get_messages(self, chat_id):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        if not self._verify_chat_owner(db, chat_id, user["id"]):
            return
        rows = db.execute(
            "SELECT id,role,content,name,args,output,token_count FROM messages WHERE chat_id=? ORDER BY created_at",
            (chat_id,),
        ).fetchall()
        msgs = []
        for r in rows:
            m = {"id": r[0], "role": r[1]}
            if r[2]: m["content"] = r[2]
            if r[3]: m["name"] = r[3]
            if r[4]:
                try: m["args"] = json.loads(r[4])
                except (json.JSONDecodeError, ValueError, TypeError): m["args"] = r[4]
            if r[5]:
                try: m["output"] = json.loads(r[5])
                except (json.JSONDecodeError, ValueError, TypeError): m["output"] = r[5]
            if r[6]: m["token_count"] = r[6]
            msgs.append(m)
        self._json_response(200, msgs)

    def _delete_last_response(self, chat_id):
        """Delete last assistant message + preceding tool calls, return last user content + previous response_id."""
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        if not self._verify_chat_owner(db, chat_id, user["id"]):
            return
        rows = db.execute(
            "SELECT id,role,content FROM messages WHERE chat_id=? ORDER BY id DESC",
            (chat_id,),
        ).fetchall()

        # Find last assistant, collect tool calls before it, find and delete the user message too
        # (resendText will re-insert the user message via /api/chat/stream)
        to_delete = []
        user_content = None
        for r in rows:
            mid, role, content = r
            if role == "assistant" and not to_delete:
                to_delete.append(mid)
            elif role == "tool" and to_delete and user_content is None:
                to_delete.append(mid)
            elif role == "user":
                user_content = content
                to_delete.append(mid)
                break
            else:
                break

        if to_delete:
            placeholders = ",".join("?" * len(to_delete))
            db.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", to_delete)

        if to_delete:
            db.execute("UPDATE chats SET response_id=NULL, updated_at=? WHERE id=?", (time.time(), chat_id))
        db.commit()
        self._json_response(200, {"user_content": user_content})

    def _truncate_messages(self, chat_id, body):
        """Delete all messages with id >= from_message_id."""
        user = self._require_auth()
        if not user:
            return
        from_id = body.get("from_message_id")
        if not from_id:
            self._error(400, "from_message_id required")
            return
        db = get_db()
        if not self._verify_chat_owner(db, chat_id, user["id"]):
            return
        db.execute("DELETE FROM messages WHERE chat_id=? AND id>=?", (chat_id, from_id))
        db.execute("UPDATE chats SET response_id=NULL, updated_at=? WHERE id=?", (time.time(), chat_id))
        db.commit()
        self._json_response(200, {"ok": True})

    def _fork_chat(self, source_id, body):
        user = self._require_auth()
        if not user:
            return
        up_to = body.get("up_to_message_id")
        if not up_to:
            self._error(400, "up_to_message_id required")
            return
        db = get_db()
        if not self._verify_chat_owner(db, source_id, user["id"]):
            return
        # Get source chat
        src = db.execute("SELECT title FROM chats WHERE id=?", (source_id,)).fetchone()
        if not src:
            self._error(404, "chat not found")
            return
        # Create new chat
        new_id = f"c{uuid.uuid4().hex[:12]}"
        new_title = f"Fork: {src[0]}"
        now = time.time()
        db.execute(
            "INSERT INTO chats (id,title,model,updated_at,user_id) VALUES (?,?,?,?,?)",
            (new_id, new_title, body.get("model", ""), now, user["id"]),
        )
        # Copy messages up to and including the specified message id
        rows = db.execute(
            "SELECT role,content,name,args,output,created_at FROM messages WHERE chat_id=? AND id<=? ORDER BY id",
            (source_id, up_to),
        ).fetchall()
        for r in rows:
            db.execute(
                "INSERT INTO messages (chat_id,role,content,name,args,output,created_at) VALUES (?,?,?,?,?,?,?)",
                (new_id, r[0], r[1], r[2], r[3], r[4], r[5]),
            )
        db.commit()
        self._json_response(200, {"id": new_id, "title": new_title})
    def _compact_chat(self, chat_id, body):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        if not self._verify_chat_owner(db, chat_id, user["id"]):
            return
        # Fetch all messages
        msg_rows = db.execute(
            "SELECT id, role, content, name, args, output FROM messages WHERE chat_id=? ORDER BY id",
            (chat_id,),
        ).fetchall()
        db_messages = []
        for r in msg_rows:
            m = {"id": r[0], "role": r[1]}
            if r[2]: m["content"] = r[2]
            if r[3]: m["name"] = r[3]
            if r[4]: m["args"] = r[4]
            if r[5]: m["output"] = r[5]
            db_messages.append(m)

        turn_count = sum(1 for m in db_messages if m["role"] in ("user", "assistant"))
        if turn_count < COMPACT_MIN_TURNS:
            return self._error(400, f"Conversation too short to compact (need at least {COMPACT_MIN_TURNS} turns).")

        # Build conversation text for summarization
        convo_snippet = self._build_convo_snippet(db_messages, max_chars=COMPACT_MAX_CHARS)

        summary_payload = {
            "model": body.get("model", ""),
            "input": (
                "Summarize this entire conversation in 3-5 sentences. "
                "Capture the key topics, decisions, code discussed, and any important context "
                "that would be needed to continue the conversation:\n\n"
                + convo_snippet
            ),
            "stream": False,
            "store": False,
        }
        try:
            sum_data = self._lmstudio_chat(summary_payload, user["id"], timeout=60)
            summary = self._extract_content(sum_data)
            if not summary:
                self._error(500, "Failed to generate summary.")
                return
            last_id = db_messages[-1]["id"]
            half = len(db_messages) // 2

            # Single transaction: update summary + delete compacted messages
            try:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "UPDATE chats SET summary=?, summary_up_to=? WHERE id=?",
                    (summary, last_id, chat_id),
                )
                ids_to_delete = [m["id"] for m in db_messages[:half]]
                if ids_to_delete:
                    placeholders = ",".join("?" * len(ids_to_delete))
                    db.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", ids_to_delete)
                db.commit()
            except Exception as e:
                try:
                    db.rollback()
                except Exception:
                    pass
                log.error(f"Compact failed: {e}")
                self._error(500, "compact failed")
                return

            # Distill insights from messages being compacted (non-blocking, separate)
            try:
                convo_for_distill = self._build_convo_snippet(db_messages[:half], max_chars=3000)
                existing = db.execute(
                    "SELECT content FROM user_insights WHERE user_id=? AND state='active'",
                    (user["id"],),
                ).fetchall()
                known = "\n".join(f"- {r[0]}" for r in existing) if existing else "(none yet)"

                distill_payload = {
                    "model": body.get("model", ""),
                    "input": self.DISTILL_PROMPT.format(
                        known_context=known, conversation=convo_for_distill
                    ),
                    "stream": False, "store": False,
                    "temperature": 0.3,
                }
                distill_data = self._lmstudio_chat(distill_payload, user["id"], timeout=30)
                raw = self._extract_content(distill_data) or ""
                new = self._parse_and_store_insights(db, user["id"], raw, chat_id)
                if new:
                    log.debug(f"memory: distilled {len(new)} insights during compaction of {chat_id}")
            except Exception as e:
                log.warning(f"memory: compaction distillation failed (non-fatal): {e}")

            self._json_response(200, {
                "summary": summary,
                "messages_summarized": len(db_messages),
                "messages_deleted": half,
            })
        except Exception as e:
            log.error(f"Compact failed: {e}")
            self._error(500, "compact failed")
    def _update_title(self, chat_id, body):
        user = self._require_auth()
        if not user:
            return
        title = body.get("title", "")
        if len(title) > TITLE_MAX_LENGTH:
            return self._error(400, f"title too long (max {TITLE_MAX_LENGTH})")
        db = get_db()
        if not self._verify_chat_owner(db, chat_id, user["id"]):
            return
        db.execute("UPDATE chats SET title=? WHERE id=?", (title, chat_id))
        db.commit()
        self._json_response(200, {"ok": True})

    def _delete_chat(self, chat_id):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        if not self._verify_chat_owner(db, chat_id, user["id"]):
            return
        db.execute("DELETE FROM shared_chats WHERE chat_id=?", (chat_id,))
        db.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
        db.execute("DELETE FROM chats WHERE id=?", (chat_id,))
        db.commit()
        self._json_response(200, {"ok": True})

    # --- Share endpoints ---

    def _share_chat(self, chat_id):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        if not self._verify_chat_owner(db, chat_id, user["id"]):
            return
        # Check if already shared
        existing = db.execute("SELECT share_id FROM shared_chats WHERE chat_id=? AND user_id=?",
                              (chat_id, user["id"])).fetchone()
        if existing:
            return self._json_response(200, {"share_id": existing[0], "url": f"/share/{existing[0]}"})
        # Build message snapshot (text only — no system prompts, no base64 images, max 500 messages)
        rows = db.execute(
            "SELECT role, content, name, args, output FROM messages WHERE chat_id=? ORDER BY id LIMIT 500",
            (chat_id,)
        ).fetchall()
        snapshot = []
        for role, content, name, args, output in rows:
            if role == "system":
                continue
            msg = {"role": role}
            if content:
                # Strip base64 image data from content
                if isinstance(content, str) and content.startswith("data:image"):
                    msg["content"] = "[image]"
                else:
                    msg["content"] = content
            if name:
                msg["name"] = name
            if args:
                msg["args"] = args
            if output:
                msg["output"] = output
            snapshot.append(msg)
        # Get chat title
        chat_row = db.execute("SELECT title FROM chats WHERE id=?", (chat_id,)).fetchone()
        title = chat_row[0] if chat_row else "Shared Chat"
        share_id = secrets.token_urlsafe(12)
        now = time.time()
        db.execute(
            "INSERT INTO shared_chats (share_id, chat_id, user_id, title, messages, created_at) VALUES (?,?,?,?,?,?)",
            (share_id, chat_id, user["id"], title, json.dumps(snapshot), now)
        )
        db.commit()
        self._json_response(200, {"share_id": share_id, "url": f"/share/{share_id}"})

    def _unshare_chat(self, chat_id):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        if not self._verify_chat_owner(db, chat_id, user["id"]):
            return
        db.execute("DELETE FROM shared_chats WHERE chat_id=? AND user_id=?", (chat_id, user["id"]))
        db.commit()
        self._json_response(200, {"ok": True})

    def _serve_shared(self, share_id):
        """Serve a read-only shared conversation page. NO auth required."""
        if not share_id:
            return self._serve_share_404()
        db = get_db()
        row = db.execute("SELECT title, messages, created_at FROM shared_chats WHERE share_id=?",
                         (share_id,)).fetchone()
        if not row:
            return self._serve_share_404()
        title, messages_json, created_at = row
        messages = json.loads(messages_json)
        html = self._build_share_html(title, messages, created_at)
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._send_security_headers(csp="default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'; base-uri 'none'", referrer="no-referrer")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _serve_share_404(self):
        html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Not Found — LM Chat</title>
<style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:-apple-system,system-ui,sans-serif;background:#0B1117;color:#E2E8F0;display:flex;align-items:center;justify-content:center;min-height:100vh}
.c{text-align:center}h1{font-size:48px;font-weight:700;color:#4A5568;margin-bottom:8px}p{color:#8494A7;font-size:15px}a{color:#C084FC;text-decoration:none}a:hover{text-decoration:underline}</style>
</head><body><div class="c"><h1>404</h1><p>This shared conversation doesn't exist or has been deleted.</p><p style="margin-top:12px"><a href="/">Go to LM Chat</a></p></div></body></html>"""
        data = html.encode("utf-8")
        self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._send_security_headers(csp="default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'", referrer="no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _build_share_html(self, title, messages, created_at):
        from datetime import datetime
        date_str = datetime.fromtimestamp(created_at).strftime("%B %d, %Y")
        # HTML-escape the title
        safe_title = html_mod.escape(title)
        msg_html = []
        def _md(text):
            """Minimal markdown: bold, italic, inline code, code blocks."""
            s = html_mod.escape(text)
            # Fenced code blocks: ```...```
            s = re.sub(r'```(\w*)\n(.*?)```', lambda m: f'<pre><code>{m.group(2)}</code></pre>', s, flags=re.DOTALL)
            # Inline code: `...`
            s = re.sub(r'`([^`]+)`', r'<code>\1</code>', s)
            # Bold: **...**
            s = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)
            # Italic: *...*
            s = re.sub(r'\*(.+?)\*', r'<em>\1</em>', s)
            # Paragraphs and line breaks
            s = s.replace("\n\n", "</p><p>").replace("\n", "<br>")
            return s
        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "") or ""
            safe = _md(content)
            if role == "user":
                msg_html.append(f'<div class="msg user"><div class="role">You</div><div class="content"><p>{safe}</p></div></div>')
            elif role == "assistant":
                msg_html.append(f'<div class="msg asst"><div class="role">Assistant</div><div class="content"><p>{safe}</p></div></div>')
            elif role == "tool":
                name = html_mod.escape(m.get("name") or "tool")
                output = m.get("output", "") or ""
                safe_output = _md(output)
                if safe_output:
                    msg_html.append(f'<div class="msg tool"><div class="role">Tool: {name}</div><div class="content tool-out"><p>{safe_output}</p></div></div>')
                else:
                    msg_html.append(f'<div class="msg tool"><div class="role">Tool: {name}</div></div>')

        return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{safe_title} — LM Chat</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,system-ui,'Segoe UI',sans-serif;background:#0B1117;color:#E2E8F0;line-height:1.65;padding:2rem 1rem}}
.container{{max-width:720px;margin:0 auto}}
h1{{font-size:1.5rem;font-weight:600;margin-bottom:4px;color:#E2E8F0}}
.meta{{font-size:0.8rem;color:#8494A7;margin-bottom:1.75rem}}
.msg{{margin-bottom:1rem;padding:0.875rem 1.125rem;border-radius:10px;border:1px solid rgba(255,255,255,.06)}}
.msg.user{{background:#131B24}}
.msg.asst{{background:#1B2530}}
.msg.tool{{background:#0D1319;font-size:0.75rem;color:#8494A7}}
.role{{font-size:0.6875rem;text-transform:uppercase;letter-spacing:.6px;color:#8494A7;margin-bottom:5px;font-weight:600}}
.content p{{margin-bottom:0.5rem}}
.content p:last-child{{margin-bottom:0}}
.tool-out{{font-size:0.75rem;color:#8494A7;margin-top:4px}}
pre{{background:#0D1319;padding:0.75rem;border-radius:8px;overflow-x:auto;font-size:0.8rem;margin:0.5rem 0;border:1px solid rgba(255,255,255,.06)}}
code{{font-family:'SF Mono',Menlo,Consolas,monospace}}
.footer{{margin-top:2rem;padding-top:1rem;border-top:1px solid rgba(255,255,255,.06);font-size:0.75rem;color:#4A5568;text-align:center}}
a{{color:#C084FC;text-decoration:none}}a:hover{{text-decoration:underline}}
</style>
</head><body>
<div class="container">
<h1>{safe_title}</h1>
<div class="meta">Shared on {date_str} via LM Chat</div>
{''.join(msg_html)}
<div class="footer">Shared from <a href="/">LM Chat</a> — local AI, your way</div>
</div>
</body></html>"""

    def _search_messages(self, body):
        user = self._require_auth()
        if not user:
            return
        query = body.get("query", "").strip()
        if not query:
            return self._error(400, "query required")

        tok = self._get_lmstudio_token(user["id"])
        query_vec = get_embedding(query, tok)
        if not query_vec:
            # Fallback to SQL LIKE search when embedding model is unavailable
            return self._search_messages_like(user, query)

        db = get_db()
        where, params = self._user_filter(user)
        rows = db.execute(f"""
            SELECT e.message_id, e.vector, m.content, m.role, m.chat_id, chats.title
            FROM embeddings e
            JOIN messages m ON e.message_id = m.id
            JOIN chats ON m.chat_id = chats.id
            {where}
            ORDER BY m.created_at DESC
            LIMIT ?
        """, (*params, SEARCH_MAX_RESULTS)).fetchall()
        results = []
        q_norm = math.sqrt(sum(x*x for x in query_vec))
        for mid, blob, content, role, chat_id, title in rows:
            vec = list(struct.unpack(f'{len(blob)//4}f', blob))
            dot = sum(a*b for a, b in zip(query_vec, vec))
            v_norm = math.sqrt(sum(x*x for x in vec))
            sim = dot / (q_norm * v_norm) if q_norm and v_norm else 0
            results.append({"message_id": mid, "score": round(sim, 4), "content": (content or "")[:200], "role": role, "chat_id": chat_id, "chat_title": title})

        results.sort(key=lambda x: x["score"], reverse=True)
        self._json_response(200, {"results": results[:20], "mode": "semantic"})

    def _search_messages_like(self, user, query):
        """Fallback text search using SQL LIKE when embedding model is unavailable."""
        db = get_db()
        # Escape LIKE wildcards in user input
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like_pattern = f"%{escaped}%"
        where, params = self._user_filter(user)
        content_filter = "AND" if where else "WHERE"
        rows = db.execute(f"""
            SELECT m.id, m.content, m.role, m.chat_id, chats.title
            FROM messages m
            JOIN chats ON m.chat_id = chats.id
            {where}
            {content_filter} m.content LIKE ? ESCAPE '\\' AND m.role IN ('user', 'assistant')
            ORDER BY m.created_at DESC
            LIMIT 20
        """, (*params, like_pattern)).fetchall()
        results = []
        for mid, content, role, chat_id, title in rows:
            results.append({"message_id": mid, "score": 1.0, "content": (content or "")[:200], "role": role, "chat_id": chat_id, "chat_title": title})
        self._json_response(200, {"results": results, "mode": "text"})

    def _user_filter(self, user, table="chats"):
        """Return (where_clause, params) for user-scoped queries."""
        if AUTH_ENABLED:
            return f"WHERE {table}.user_id = ?", (user["id"],)
        return "", ()

    # --- Helpers ---

    def _lmstudio_chat(self, payload, user_id, timeout=60):
        """Send a chat completion request to LM Studio. Returns parsed JSON."""
        headers = {"Content-Type": "application/json"}
        token = self._get_lmstudio_token(user_id)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            f"{LMSTUDIO}/api/v1/chat",
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    def _get_lmstudio_token(self, user_id=None):
        """Get LM Studio auth token from server-side user settings, fall back to env var.
        No longer reads from client Authorization header (H4 security fix)."""
        if user_id:
            stored = self._get_user_lm_apikey(user_id)
            if stored:
                return stored
        return LMSTUDIO_TOKEN

    def _extract_content(self, data):
        if data.get("content"): return data["content"]
        if data.get("output_text"): return data["output_text"]
        if isinstance(data.get("output"), str): return data["output"]
        if isinstance(data.get("output"), list):
            t = "\n".join(i.get("content", "") for i in data["output"] if i.get("type") == "message")
            if t: return t
        if data.get("choices"):
            return data["choices"][0].get("message", {}).get("content", "")
        return ""

    def _error(self, code, msg):
        """Send a JSON error response."""
        self._json_response(code, {"error": msg})

    def _build_lmstudio_payload(self, body, system_prompt=None, stream=False):
        """Build the common LM Studio API payload."""
        integrations = body.get("integrations", DEFAULT_INTEGRATIONS)
        payload = {
            "model": body.get("model", ""),
            "input": body.get("input", ""),
            "integrations": integrations,
            "stream": stream,
        }
        if body.get("previous_response_id"):
            payload["previous_response_id"] = body["previous_response_id"]
        if system_prompt:
            payload["system_prompt"] = system_prompt
        elif body.get("system_prompt"):
            payload["system_prompt"] = body["system_prompt"]
        if body.get("temperature") is not None:
            payload["temperature"] = body["temperature"]
        if body.get("top_p") is not None:
            payload["top_p"] = body["top_p"]
        if body.get("top_k") is not None:
            payload["top_k"] = body["top_k"]
        if body.get("min_p") is not None:
            payload["min_p"] = body["min_p"]
        if body.get("repeat_penalty") is not None:
            payload["repeat_penalty"] = body["repeat_penalty"]
        if body.get("max_output_tokens") is not None and body["max_output_tokens"] > 0:
            payload["max_output_tokens"] = body["max_output_tokens"]
        if body.get("reasoning"):
            payload["reasoning"] = body["reasoning"]
        if body.get("context_length") is not None and body["context_length"] > 0:
            payload["context_length"] = body["context_length"]
        if body.get("incognito"):
            payload["store"] = False
        return payload

    def _build_convo_snippet(self, messages, max_chars=4000):
        """Build conversation text for summarization."""
        convo_text = []
        for m in messages:
            role = m.get("role", "")
            text = m.get("content") or ""
            if role == "tool":
                text = f"[tool: {m.get('name', '')}] {(m.get('output') or '')[:200]}"
            if text:
                convo_text.append(f"{role}: {text}")
        return "\n".join(convo_text)[:max_chars]

    # --- Memory: Insight scoring and retrieval ---

    CATEGORY_WEIGHTS = {
        "identity": 2.0, "preference": 2.0, "opinion": 1.5,
        "skill": 1.0, "project": 1.0, "context": 1.0,
    }

    def _score_insights(self, db, user_id):
        """Update freshness scores for all active insights. Pure SQL, no LLM."""
        db.execute("""
            UPDATE user_insights SET state = 'faded'
            WHERE user_id = ? AND state = 'active'
            AND (1.0 / (1.0 + (julianday('now') - julianday(last_used, 'unixepoch')) / 30.0))
                * (1.0 + 0.3 * ln(1 + use_count)) < 0.1
        """, (user_id,))
        db.commit()

    def _get_top_insights(self, db, user_id, limit=30, max_tokens=500):
        """Retrieve top-N active insights sorted by weighted freshness score."""
        self._score_insights(db, user_id)
        rows = db.execute("""
            SELECT id, content, category,
                (1.0 / (1.0 + (julianday('now') - julianday(last_used, 'unixepoch')) / 30.0))
                * (1.0 + 0.3 * ln(1 + use_count)) AS score
            FROM user_insights
            WHERE user_id = ? AND state = 'active'
            ORDER BY score DESC
            LIMIT ?
        """, (user_id, limit * 2)).fetchall()

        weighted = []
        for r in rows:
            cat_w = self.CATEGORY_WEIGHTS.get(r[2], 1.0)
            weighted.append({"id": r[0], "content": r[1], "category": r[2],
                             "score": r[3] * cat_w})
        weighted.sort(key=lambda x: x["score"], reverse=True)

        result, chars = [], 0
        for w in weighted[:limit]:
            chars += len(w["content"]) + 20
            if chars > max_tokens * 4:
                break
            result.append(w)
        return result

    def _format_insights_for_prompt(self, insights):
        """Format insights as a system prompt section."""
        if not insights:
            return ""
        lines = ["## About you"]
        for ins in insights:
            lines.append(f"- {ins['content']} [{ins['category']}]")
        return "\n".join(lines)

    def _touch_insights(self, db, insight_ids):
        """Bump last_used and use_count for retrieved insights."""
        if not insight_ids:
            return
        placeholders = ",".join("?" * len(insight_ids))
        db.execute(f"""
            UPDATE user_insights SET last_used = ?, use_count = use_count + 1
            WHERE id IN ({placeholders})
        """, [time.time()] + insight_ids)
        db.commit()

    # --- Memory: Insight parsing ---

    VALID_INSIGHT_CATEGORIES = {"identity", "preference", "skill", "project", "opinion", "context"}

    def _parse_insight_line(self, line):
        """Parse a single insight line. Returns (content, category) or (None, None)."""
        line = line.lstrip("•-* ").strip()
        if not line:
            return None, None
        category = "context"
        for cat in self.VALID_INSIGHT_CATEGORIES:
            if f"[{cat}]" in line.lower():
                category = cat
                line = re.sub(r'\[' + cat + r'\]', '', line, flags=re.IGNORECASE).strip()
                break
        return line, category

    def _parse_and_store_insights(self, db, user_id, raw_text, chat_id=None):
        """Parse LLM distillation output and store insights. Returns list of new insights."""
        if not raw_text or raw_text.strip().lower() == "none":
            return []
        new_insights = []
        now = time.time()
        for raw_line in raw_text.strip().split("\n"):
            raw_line = raw_line.strip()
            if raw_line.lower().lstrip("- \u2022").startswith("[correction]"):
                raw_line = raw_line.lstrip("- \u2022")[len("[correction]"):].strip()
            line, category = self._parse_insight_line(raw_line)
            if not line or len(line) < 5:
                continue
            insight_id = uuid.uuid4().hex[:12]
            db.execute(
                """INSERT INTO user_insights (id, user_id, content, category, origin_chat_id, created_at, last_used)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (insight_id, user_id, line, category, chat_id, now, now),
            )
            new_insights.append({"id": insight_id, "content": line, "category": category})
        if new_insights:
            db.commit()
        return new_insights

    # --- Memory: Insight distillation ---

    DISTILL_PROMPT = """You are a personal context system. Given this conversation, distill NEW insights about the user.

Rules:
- Only insights about the USER (preferences, identity, work, skills, opinions, style)
- One insight per line, concise (under 20 words)
- Tag each: [identity] [preference] [skill] [project] [opinion] [context]
- Skip anything generic or already known
- If correcting a previous insight, prefix with [correction]

Known context (do not repeat):
{known_context}

Conversation:
{conversation}

Distill insights (or respond "none" if nothing new):"""

    def _distill_insights(self, body):
        """Extract user insights from a chat conversation via LLM call."""
        user = self._require_auth()
        if not user:
            return

        chat_id = body.get("chat_id")
        model = body.get("model", "")
        if not chat_id:
            return self._error(400, "chat_id required")

        db = get_db()
        if not self._verify_chat_owner(db, chat_id, user["id"]):
            return

        # Fetch conversation
        msg_rows = db.execute(
            "SELECT role, content, name, output FROM messages WHERE chat_id=? ORDER BY id",
            (chat_id,),
        ).fetchall()
        if not msg_rows:
            return self._json_response(200, {"insights": [], "message": "empty chat"})

        convo_lines = []
        for r in msg_rows:
            role, content = r[0], r[1] or ""
            if role == "tool":
                name = r[2] or "tool"
                output = r[3] or ""
                convo_lines.append(f"[Tool: {name}] {output[:200]}")
            elif content:
                convo_lines.append(f"{role}: {content}")
        conversation = "\n".join(convo_lines)[-3000:]  # last 3000 chars

        # Fetch existing insights for dedup
        existing = db.execute(
            "SELECT content FROM user_insights WHERE user_id=? AND state='active'",
            (user["id"],),
        ).fetchall()
        known_context = "\n".join(f"- {r[0]}" for r in existing) if existing else "(none yet)"

        # LLM call
        prompt = self.DISTILL_PROMPT.format(
            known_context=known_context, conversation=conversation
        )
        payload = {
            "model": model,
            "input": prompt,
            "stream": False,
            "store": False,
            "temperature": 0.3,
        }
        try:
            data = self._lmstudio_chat(payload, user["id"], timeout=60)
            raw_text = self._extract_content(data) or ""
        except Exception as e:
            log.error(f"LLM distillation failed: {e}")
            return self._error(502, "LLM distillation failed")

        try:
            new_insights = self._parse_and_store_insights(db, user["id"], raw_text, chat_id)
        except Exception as e:
            log.error(f"Failed to store insights: {e}")
            return self._error(500, "failed to store insights")
        log.debug(f"memory: distilled {len(new_insights)} insights from chat {chat_id}")
        self._json_response(200, {"insights": new_insights})

    # --- Memory: CRUD endpoints ---

    def _list_insights(self):
        """GET /api/insights — list all insights for current user."""
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        rows = db.execute(
            """SELECT id, content, category, origin_chat_id, weight,
                      created_at, last_used, use_count, state
               FROM user_insights WHERE user_id=? AND state != 'removed'
               ORDER BY created_at DESC""",
            (user["id"],),
        ).fetchall()
        insights = [
            {"id": r[0], "content": r[1], "category": r[2], "origin_chat_id": r[3],
             "weight": r[4], "created_at": r[5], "last_used": r[6],
             "use_count": r[7], "state": r[8]}
            for r in rows
        ]
        self._json_response(200, insights)

    def _add_insight(self, body):
        """POST /api/insights — manually add an insight."""
        user = self._require_auth()
        if not user:
            return
        content = (body.get("content") or "").strip()
        if not content:
            return self._error(400, "content required")
        category = body.get("category", "context")
        if category not in self.VALID_INSIGHT_CATEGORIES:
            category = "context"
        now = time.time()
        insight_id = uuid.uuid4().hex[:12]
        db = get_db()
        db.execute(
            """INSERT INTO user_insights (id, user_id, content, category, created_at, last_used)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (insight_id, user["id"], content, category, now, now),
        )
        db.commit()
        self._json_response(201, {"id": insight_id, "content": content, "category": category})

    def _edit_insight(self, insight_id, body):
        """POST /api/insights/:id/edit — edit an insight."""
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        row = db.execute(
            "SELECT user_id FROM user_insights WHERE id=?", (insight_id,)
        ).fetchone()
        if not row or row[0] != user["id"]:
            return self._error(404, "insight not found")
        updates = []
        params = []
        if "content" in body:
            updates.append("content=?")
            params.append((body["content"] or "").strip())
        if "category" in body:
            cat = body["category"] if body["category"] in self.VALID_INSIGHT_CATEGORIES else "context"
            updates.append("category=?")
            params.append(cat)
        if not updates:
            return self._error(400, "nothing to update")
        params.append(insight_id)
        db.execute(f"UPDATE user_insights SET {','.join(updates)} WHERE id=?", params)
        db.commit()
        self._json_response(200, {"ok": True})

    def _delete_insight(self, insight_id):
        """DELETE /api/insights/:id — hard delete an insight."""
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        row = db.execute(
            "SELECT user_id FROM user_insights WHERE id=?", (insight_id,)
        ).fetchone()
        if not row or row[0] != user["id"]:
            return self._error(404, "insight not found")
        db.execute("DELETE FROM user_insights WHERE id=?", (insight_id,))
        db.commit()
        self._json_response(200, {"ok": True})

    def _clear_insights(self):
        """DELETE /api/insights — clear all insights for current user."""
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        db.execute("DELETE FROM user_insights WHERE user_id=?", (user["id"],))
        db.commit()
        self._json_response(200, {"ok": True})

    def _get_insight_settings(self):
        """GET /api/insights/settings — get memory preferences."""
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        rows = db.execute(
            "SELECT key, value FROM user_settings WHERE user_id=? AND key LIKE 'memory_%'",
            (user["id"],),
        ).fetchall()
        settings = {r[0]: r[1] for r in rows}
        settings.setdefault("memory_enabled", "true")
        settings.setdefault("memory_max_inject", "30")
        self._json_response(200, settings)

    def _save_insight_settings(self, body):
        """POST /api/insights/settings — update memory preferences."""
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        allowed = {"memory_enabled", "memory_max_inject"}
        for key, value in body.items():
            if key not in allowed:
                continue
            val = str(value)
            if key == "memory_enabled" and val not in ("true", "false"):
                continue
            if key == "memory_max_inject":
                try:
                    n = int(val)
                    if n < 1 or n > 100:
                        continue
                    val = str(n)
                except (ValueError, TypeError):
                    continue
            db.execute(
                "INSERT OR REPLACE INTO user_settings (user_id, key, value) VALUES (?, ?, ?)",
                (user["id"], key, val),
            )
        db.commit()
        self._json_response(200, {"ok": True})

    REFINE_PROMPT = """You are a personal context curator. Review these user insights and:

1. Merge duplicates (keep the more specific version)
2. Resolve contradictions (newer insight wins, note the update)
3. Group related insights by theme
4. Remove insights that are clearly session-specific and not worth keeping

Current insights:
{insights}

Return ONLY the curated list. One insight per line with category tag.
Prefix removed items with [drop] and state the reason briefly.
Prefix merged items with [merged] and include all relevant detail.
Keep items unchanged if they're fine.

Curated list:"""

    def _refine_insights(self, body):
        """POST /api/insights/refine — merge, dedup, prune via LLM."""
        user = self._require_auth()
        if not user:
            return
        model = body.get("model", "")

        db = get_db()
        rows = db.execute(
            "SELECT id, content, category, created_at FROM user_insights WHERE user_id=? AND state='active' ORDER BY created_at",
            (user["id"],),
        ).fetchall()

        if len(rows) < 3:
            return self._json_response(200, {"message": "too few insights to refine", "count": len(rows)})

        insight_lines = []
        for r in rows:
            date_str = time.strftime("%Y-%m-%d", time.localtime(r[3]))
            insight_lines.append(f"- {r[1]} [{r[2]}] (added {date_str})")

        prompt = self.REFINE_PROMPT.format(insights="\n".join(insight_lines))

        payload = {
            "model": model,
            "input": prompt,
            "stream": False,
            "store": False,
            "temperature": 0.2,
        }
        try:
            data = self._lmstudio_chat(payload, user["id"], timeout=60)
            raw_text = self._extract_content(data) or ""
        except Exception as e:
            return self._error(502, f"LLM refinement failed: {e}")

        dropped, merged, kept = 0, 0, 0
        now = time.time()

        old_ids = [r[0] for r in rows]

        new_insights = []
        for raw_line in raw_text.strip().split("\n"):
            raw_line = raw_line.strip()
            if not raw_line or len(raw_line.lstrip("- •")) < 5:
                continue

            if raw_line.lstrip("- •").lower().startswith("[drop]"):
                dropped += 1
                continue

            is_merged = raw_line.lstrip("- •").lower().startswith("[merged]")
            if is_merged:
                raw_line = raw_line.lstrip("- •")[len("[merged]"):].strip()
                merged += 1
            else:
                kept += 1

            line, category = self._parse_insight_line(raw_line)

            # Strip date annotations the LLM might echo back
            line = re.sub(r'\(added \d{4}-\d{2}-\d{2}\)', '', line).strip() if line else ""

            if line and len(line) >= 5:
                new_insights.append({"content": line, "category": category})

        # Sanity check: abort if LLM returned too few insights (likely garbage)
        if len(new_insights) == 0:
            return self._error(502, "Refinement produced no usable insights — aborted to protect existing data")
        if len(new_insights) < len(old_ids) * 0.3:
            return self._error(502, f"Refinement produced too few insights ({len(new_insights)} vs {len(old_ids)}) — aborted")

        # Replace: mark old as removed, insert new (atomic transaction)
        try:
            placeholders = ",".join("?" * len(old_ids))
            db.execute(f"UPDATE user_insights SET state='removed' WHERE id IN ({placeholders})", old_ids)
            for ins in new_insights:
                insight_id = uuid.uuid4().hex[:12]
                db.execute(
                    """INSERT INTO user_insights (id, user_id, content, category, created_at, last_used)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (insight_id, user["id"], ins["content"], ins["category"], now, now),
                )
            db.commit()
        except Exception as e:
            db.rollback()
            log.error(f"Refinement DB write failed: {e}")
            return self._error(500, "refinement failed")
        result = {"dropped": dropped, "merged": merged, "kept": kept, "total": len(new_insights)}
        log.debug(f"memory: refined: {result}")
        self._json_response(200, result)

    def _persist_chat_messages(self, db, chat_id, user_input, content, tool_calls, response_id, usage, now=None):
        """Persist user message, tool calls, and assistant response."""
        if not now:
            now = time.time()
        prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        completion_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        # Extract text from array input (don't store base64 image data)
        if isinstance(user_input, list):
            text_parts = [item.get("text", "") or item.get("content", "") for item in user_input if item.get("type") in ("text", "message")]
            image_count = sum(1 for item in user_input if item.get("type") == "image")
            persist_text = "\n".join(text_parts)
            if image_count:
                prefix = "\n" if persist_text else ""
                persist_text += f"{prefix}[{image_count} image{'s' if image_count > 1 else ''} attached]"
        else:
            persist_text = user_input
        db.execute(
            "INSERT INTO messages (chat_id,role,content,token_count,created_at) VALUES (?,?,?,?,?)",
            (chat_id, "user", persist_text, prompt_tokens or None, now),
        )
        for tc in tool_calls:
            db.execute(
                "INSERT INTO messages (chat_id,role,name,args,output,created_at) VALUES (?,?,?,?,?,?)",
                (chat_id, "tool", tc.get("tool", ""),
                 json.dumps(tc.get("arguments")) if tc.get("arguments") else None,
                 json.dumps(tc.get("output")) if tc.get("output") else None,
                 now),
            )
        if content:
            db.execute(
                "INSERT INTO messages (chat_id,role,content,token_count,created_at) VALUES (?,?,?,?,?)",
                (chat_id, "assistant", content, completion_tokens or None, now),
            )
        db.execute(
            "UPDATE chats SET response_id=?, updated_at=? WHERE id=?",
            (response_id, now, chat_id),
        )
        db.commit()

    def _json_response(self, code, data):
        if isinstance(data, (dict, list)):
            data = json.dumps(data).encode()
        elif isinstance(data, str): data = data.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(data)

    _file_cache = {}  # {filename: (raw_bytes, gzipped_bytes, mtime, etag)}

    def _serve_file(self, filename, content_type):
        path = os.path.join(os.path.dirname(__file__), filename)
        try:
            st = os.stat(path)
        except FileNotFoundError:
            self.send_error(404)
            return
        cached = self._file_cache.get(filename)
        if cached and cached[2] == st.st_mtime:
            raw, gz, etag = cached[0], cached[1], cached[3]
        else:
            with open(path, "rb") as f:
                raw = f.read()
            gz = gzip.compress(raw, compresslevel=6)
            etag = hashlib.md5(raw).hexdigest()
            self._file_cache[filename] = (raw, gz, st.st_mtime, etag)
        # ETag: return 304 if client has current version
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.end_headers()
            return
        accept_enc = self.headers.get("Accept-Encoding", "")
        use_gz = "gzip" in accept_enc and len(gz) < len(raw)
        data = gz if use_gz else raw
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("ETag", etag)
        if use_gz:
            self.send_header("Content-Encoding", "gzip")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _proxy_get(self, path, user_id=None):
        headers = {}
        token = self._get_lmstudio_token(user_id)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(f"{LMSTUDIO}{path}", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                self._json_response(200, resp.read())
        except Exception as e:
            log.error(f"proxy GET: {e}")
            self._error(502, "upstream service unavailable")

    def log_message(self, format, *args):
        log.debug(f"HTTP {args[0]}")


def _pid_file():
    """Return PID file path next to the database."""
    return os.path.join(os.path.dirname(DB_PATH) or ".", ".lm_chat.pid")

def _kill_stale_server():
    """If a previous lm-chat is still on our port, kill it."""
    pidfile = _pid_file()
    # Check PID file first
    if os.path.exists(pidfile):
        try:
            old_pid = int(open(pidfile).read().strip())
            if old_pid != os.getpid():
                os.kill(old_pid, signal.SIGTERM)
                log.info(f"Stopped previous lm-chat (PID {old_pid})")
                for _ in range(20):  # wait up to 2s
                    time.sleep(0.1)
                    try:
                        os.kill(old_pid, 0)
                    except OSError:
                        break
                else:
                    os.kill(old_pid, signal.SIGKILL)
                    log.warning(f"Force-killed stale lm-chat (PID {old_pid})")
        except (ValueError, OSError):
            pass  # stale file, process already gone
    # Fallback: check if anything is on our port (covers non-PID-file cases)
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(1)
        s.connect(("127.0.0.1", PORT))
        s.close()
        # Something is listening — try to identify and kill it
        try:
            import subprocess
            out = subprocess.check_output(["lsof", "-ti", f":{PORT}"], text=True).strip()
            for pid_str in out.splitlines():
                pid = int(pid_str)
                if pid != os.getpid():
                    os.kill(pid, signal.SIGTERM)
                    log.info(f"Stopped process {pid} on port {PORT}")
                    time.sleep(0.5)
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            log.warning(f"Port {PORT} is in use but couldn't identify the process — start may fail")
    except (ConnectionRefusedError, OSError):
        pass  # port is free
    finally:
        s.close()

def _write_pid():
    """Write current PID to file."""
    try:
        with open(_pid_file(), "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass

def _remove_pid():
    """Remove PID file on shutdown."""
    try:
        os.remove(_pid_file())
    except OSError:
        pass

if __name__ == "__main__":
    init_db()
    _kill_stale_server()
    server = PooledHTTPServer(("0.0.0.0", PORT), Handler)
    _write_pid()
    log.info(f"lm-chat running on http://localhost:{PORT}")
    log.info(f"Proxying to LM Studio at {LMSTUDIO}")
    log.info(f"Chat DB: {DB_PATH}")
    log.info(f"Logs: {LOG_DIR} (max {LOG_MAX_BYTES // 1024 // 1024}MB x {LOG_BACKUP_COUNT} files)")
    if AUTH_ENABLED:
        log.info("Authentication: ENABLED (set LM_CHAT_AUTH=false to disable)")
        db = get_db()
        count = db.execute("SELECT COUNT(*) FROM users WHERE username != 'default'").fetchone()[0]
        if count == 0:
            # Auto-provision default admin account
            admin_user = os.environ.get("LM_CHAT_ADMIN_USER", "admin")
            admin_pass = os.environ.get("LM_CHAT_ADMIN_PASS", "")
            if not admin_pass:
                admin_pass = secrets.token_urlsafe(12)
                # Print credentials to stderr only — never to log files
                print(f"\n{'='*50}", file=sys.stderr)
                print("  Admin account created", file=sys.stderr)
                print(f"  Username: {admin_user}", file=sys.stderr)
                print(f"  Password: {admin_pass}", file=sys.stderr)
                print("  (set LM_CHAT_ADMIN_PASS to use your own)", file=sys.stderr)
                print(f"{'='*50}\n", file=sys.stderr)
            else:
                log.info(f"Admin account created: {admin_user}")
            admin_id = uuid.uuid4().hex
            pw_hash, salt = hash_password(admin_pass)
            db.execute(
                "INSERT INTO users (id,username,password_hash,salt,display_name,is_admin,created_at) VALUES (?,?,?,?,?,?,?)",
                (admin_id, admin_user, pw_hash, salt, admin_user, 1, time.time()),
            )
            # Migrate any data from auth-disabled "default" user to new admin
            migrated = 0
            KNOWN_TABLES = {"chats", "messages", "embeddings", "user_settings", "user_insights", "shared_chats"}
            for table in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                tname = table[0]
                if tname not in KNOWN_TABLES:
                    continue
                cols = [r[1] for r in db.execute(f"PRAGMA table_info({tname})").fetchall()]
                if "user_id" in cols:
                    r = db.execute(f"UPDATE {tname} SET user_id=? WHERE user_id='default'", (admin_id,))
                    migrated += r.rowcount
            if migrated:
                log.info(f"Migrated {migrated} rows from default user to {admin_user}")
            db.commit()
    else:
        log.info("Authentication: disabled (set LM_CHAT_AUTH=true to enable)")

    def shutdown_handler(signum, frame):
        log.info("Shutting down gracefully...")
        server.shutdown()

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        _remove_pid()
        log.info("Server stopped.")
