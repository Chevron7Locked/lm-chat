# Security Policy

## Reporting a Vulnerability

Email **security@chevron7.io** with:

- **Description:** What is the vulnerability?
- **Affected version:** Which version(s)?
- **Proof of concept:** Reproduction steps or a test case
- **Severity:** Your assessment (critical / high / medium / low)

Response timeline:
- Acknowledgement within **48 hours**
- Status update within **7 days**
- Fix and coordinated disclosure once patched
- Credit in release notes (unless you prefer anonymity)

Please do not publicly disclose before a patch is available.

## Scope

**In scope:**
- `server.py`, `app.js`, `index.html`, `style.css`
- SQLite schema and data handling
- Authentication (TOTP, password hashing, session cookies)
- API endpoints and request handling

**Out of scope:**
- LM Studio itself — report to the [LM Studio team](https://lmstudio.ai)
- External MCP server vulnerabilities
- Model output safety or jailbreaks

## Security Features

| Feature | Implementation |
|---------|---------------|
| Password hashing | Scrypt with composite hash format `scrypt$n=N$r=R$p=P$<hex>` (default `n=131072` per OWASP 2024). Legacy bare-hex hashes from pre-0.5.0 still verify and are silently upgraded on the next successful login. Length-capped at 256 chars before hashing to bound CPU per login attempt. Constant-time compare via `hmac.compare_digest`. |
| Sessions | HttpOnly + SameSite=Strict cookies, SHA-256 token storage. `__Host-` prefixed when over HTTPS. 30-day expiry with sliding-window renewal past 50% of lifetime. Optional rotate-on-login via `LM_CHAT_SINGLE_SESSION=true` (revokes the user's other sessions atomically). Password change always revokes all of the user's sessions. |
| At-rest encryption | `user_settings.lm_apikey` and `user_settings.remote_mcps` are authenticated-encrypted with SHAKE-256 (stream) + HMAC-SHA256 (MAC), keys derived from `LM_CHAT_SECRET` via HKDF. Stored as `enc$v1$<base64>`. Per-context key separation so an attacker who recovers one column's plaintext gets no leverage on the other. Stdlib-only — no `cryptography` dependency. |
| CSRF protection | Custom request header validation (`X-Requested-With: lm-chat`) on POST/DELETE/PATCH |
| First-visitor admin bootstrap | `LM_CHAT_SETUP_TOKEN` env (optional) — when set, `/api/auth/setup` requires the matching token to prevent a public-URL attacker from racing the operator to create the first admin. |
| Auth-disabled mode safety | Server refuses to start with `LM_CHAT_AUTH=false` against a database that already contains non-default users (would otherwise expose every user's chats to every visitor). |
| TLS hardening | `LM_CHAT_HSTS=true` (or `preload`) emits Strict-Transport-Security when serving over HTTPS (`LM_CHAT_HTTPS=1` or `X-Forwarded-Proto: https`). Default 2-year max-age, override via `LM_CHAT_HSTS_MAX_AGE`. |
| SQL injection | Parameterized queries (`?`) throughout |
| XSS | HTML escaping in all renders. Content Security Policy: `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'`. `'unsafe-inline'` on `style-src` is required because the JS templates set `style="..."` inline; tighten with a nonce if you fork. |
| Rate limiting | Login attempts (5 per 15 min per IP, scrypt-bounded) + per-user API rate limiting (token bucket, 2/s sustained, 60 burst) |
| Container | Non-root user, read-only root filesystem, all capabilities dropped, base image pinned by manifest digest, `PYTHONDONTWRITEBYTECODE=1` for runtime immutability, SBOM + SLSA-style provenance attached on every release. |
| CI supply chain | All GitHub Actions pinned by commit SHA with version comments (OpenSSF Scorecard compliant); Dependabot updates SHA and comment atomically. |
| Observability | Per-request 12-char `request_id` echoed in JSON error responses and the access log so bug reports can be cross-referenced through every log line. |

## Environment variables

| Variable | Purpose |
|---|---|
| `LM_CHAT_AUTH` | `true` (default) enables auth.  `false` runs single-user trusted mode — refuses to start on a multi-user DB. |
| `LM_CHAT_ADMIN_USER` / `_PASS` | Override the auto-provisioned admin credentials on first run. |
| `LM_CHAT_SECRET` | 64-hex-char signing key.  Auto-generated to `.lm_chat_secret` on first run; back it up — losing it means losing every encrypted secret and signed partial token. |
| `LM_CHAT_SETUP_TOKEN` | When set, `/api/auth/setup` requires `body["setup_token"]` to match.  Use on public URLs. |
| `LM_CHAT_SINGLE_SESSION` | `true`/`1`/`on`/`yes` → fresh login revokes other sessions for the same user. |
| `LM_CHAT_HSTS` | `true`/`on`/`1` → standard HSTS directive over HTTPS.  `preload` → also include the `preload` token. |
| `LM_CHAT_HSTS_MAX_AGE` | Integer seconds.  Defaults to 63072000 (2 years). |
| `LM_CHAT_HTTPS` | Set when the server itself terminates TLS; otherwise the reverse proxy sets `X-Forwarded-Proto: https`. |
| `LM_CHAT_SCRYPT_N` / `_R` / `_P` | Override scrypt cost parameters.  Defaults: `n=131072`, `r=8`, `p=1`. |
| `LM_CHAT_DB` | SQLite file path. |
| `LMSTUDIO_URL` | Upstream LM Studio endpoint. |
| `LMSTUDIO_TOKEN` | Optional global LM Studio bearer token (per-user keys take precedence). |

## Contact

- Security issues: security@chevron7.io
- General: dev@chevron7.io
