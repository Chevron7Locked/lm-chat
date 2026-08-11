# Security Policy

## Reporting a Vulnerability

Email **security@chevron7.io** with:

- **Description:** What is the vulnerability?
- **Affected version:** Which release(s)?
- **Proof of concept:** Reproduction steps or a test case
- **Severity:** Your assessment (critical / high / medium / low)

Response timeline:

- Acknowledgement within **48 hours**
- Status update within **7 days**
- Fix and coordinated disclosure once patched
- Credit in the release notes (unless you prefer anonymity)

Please do not publicly disclose before a patch is available.

## Scope

LM Chat is self-hosted and single-instance by design — a handful of trusted
people share one deployment. In scope:

- Authentication and session handling, TOTP two-factor, the invite flow
- Per-user data isolation (chats, projects, documents, memory)
- Provider-credential handling (encrypted at rest, never returned to the browser)
- The MCP tool runtime and its filesystem sandbox
- SSRF, injection, and the streaming/upload surfaces

Out of scope: issues that require an already-compromised admin account or host,
and anything in the internal `docs/` tree (not shipped).

## Supported versions

Security fixes land on the latest release. Pin to a released tag
(`ghcr.io/chevron7locked/lm-chat:<version>`) rather than `:latest` if you
need reproducible deployments.
