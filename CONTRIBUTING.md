# Contributing to lm-chat

Contributions are welcome. This is a focused, zero-dependency project — please read before opening a PR.

## Setup

```bash
git clone https://github.com/chevron7locked/lm-chat.git
cd lm-chat
python3 server.py          # start server (no install needed)
```

Tests run with pytest:

```bash
pip install pytest pytest-httpx pytest-cov
pytest tests/ -v
```

## Ground Rules

1. **No new external dependencies.** stdlib Python only, vanilla JS only. This is a hard constraint.
2. **Discuss features first.** Open an issue before building. Avoids wasted effort.
3. **Bug fixes are always welcome** without discussion.
4. **Security issues:** See [SECURITY.md](./SECURITY.md) — do not open a public issue.

## Code Style

- Python: PEP 8, enforced by Ruff (`ruff check server.py`)
- JavaScript: ES2022 modules, no transpilation, no bundler
- CSS: `@layer` architecture, CSS custom properties, hex/rgba only (no oklch)
- Commits: Conventional Commits (`feat:`, `fix:`, `refactor:`, `docs:`, `chore:`)

## Submitting a PR

1. Fork, create a branch (`fix/your-fix` or `feat/your-feature`)
2. Make focused changes — one concern per PR
3. Run `pytest tests/ -v` and `ruff check server.py` — both must pass
4. Open a PR against `main` with a clear description

## License

By contributing, you agree that your contributions are licensed under AGPL-3.0.
For commercial licensing inquiries, contact dev@chevron7.io.
