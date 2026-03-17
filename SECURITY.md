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
| Password hashing | Scrypt (`hashlib.scrypt`), timing-safe compare (`hmac.compare_digest`) |
| Sessions | HttpOnly + SameSite=Strict cookies, scrypt-hashed token storage |
| CSRF protection | Custom request header validation (`X-Requested-With: lm-chat`) on POST/DELETE/PATCH |
| SQL injection | Parameterized queries (`?`) throughout |
| XSS | HTML escaping in all renders + strict Content Security Policy |
| Rate limiting | Login attempts (5 per 15 min per IP) + per-user API rate limiting |
| Container | Non-root user, read-only root filesystem, all capabilities dropped |

## Contact

- Security issues: security@chevron7.io
- General: dev@chevron7.io
