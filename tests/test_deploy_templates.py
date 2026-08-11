# SPDX-License-Identifier: Apache-2.0
"""Deploy-template env-var binding tests.

Parses each deploy/*.example env file, the env: blocks in
deploy/docker-compose.yml, and the env[].name entries in
deploy/k8s/deployment.yaml.

For each KEY found, asserts the key is either:
  (a) a recognised Settings field (Settings.model_fields, lowercased), OR
  (b) in the POSTGRES_PASSTHROUGH whitelist (keys belonging to the postgres
      container's own configuration, not to lm-chat's Settings).

On failure, lists unrecognised keys plus the closest Settings field name
(via difflib.get_close_matches) to aid rapid fixing.

Closes: deploy env var binding gap (HIGH).
"""
from __future__ import annotations

import difflib
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Whitelist of keys that appear in deploy/ files but are NOT Settings fields.
# These belong to the postgres container's own initialisation or to Docker
# infrastructure keys — not to lm-chat's Pydantic Settings.
# ---------------------------------------------------------------------------
_PASSTHROUGH_KEYS: frozenset[str] = frozenset(
    {
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
    }
)

REPO_ROOT = Path(__file__).parent.parent
DEPLOY_DIR = REPO_ROOT / "deploy"


def _settings_field_names() -> frozenset[str]:
    """Return the set of recognised field names from Settings (uppercased).

    pydantic-settings maps env-var names to field names case-insensitively.
    We compare in uppercase to match the convention used in .env files.
    """
    from lmchat.config import Settings

    # model_fields keys are the Python attribute names (lowercase).
    # Env vars are uppercase. Both sides are normalised to uppercase for comparison.
    return frozenset(k.upper() for k in Settings.model_fields)


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a KEY=value env file, ignoring blank lines and comments."""
    result: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, _ = line.partition("=")
            result[key.strip()] = ""
    return result


def _parse_docker_compose_env(path: Path) -> dict[str, str]:
    """Return env keys from all services.*.environment blocks."""
    data = yaml.safe_load(path.read_text())
    result: dict[str, str] = {}
    services = data.get("services", {})
    for _svc_name, svc in services.items():
        env = svc.get("environment", {})
        if isinstance(env, dict):
            for k in env:
                result[str(k)] = ""
        elif isinstance(env, list):
            for entry in env:
                if isinstance(entry, str) and "=" in entry:
                    k, _, _ = entry.partition("=")
                    result[k.strip()] = ""
                elif isinstance(entry, str):
                    result[entry.strip()] = ""
    return result


def _parse_k8s_deployment_env(path: Path) -> dict[str, str]:
    """Return env var names from spec.template.spec.containers[].env[].name."""
    result: dict[str, str] = {}
    data = yaml.safe_load(path.read_text())
    spec = (
        data.get("spec", {})
        .get("template", {})
        .get("spec", {})
    )
    for container in spec.get("containers", []):
        for env_entry in container.get("env", []):
            name = env_entry.get("name")
            if name:
                result[str(name)] = ""
    return result


def _assert_all_recognised(keys: dict[str, str], source_label: str) -> None:
    """Assert every key in *keys* is a Settings field or in the passthrough set.

    Reports unknown keys with nearest-match suggestions on failure.
    """
    recognised = _settings_field_names()
    unknown: list[str] = []
    for key in keys:
        if key in _PASSTHROUGH_KEYS:
            continue
        if key.upper() in recognised:
            continue
        unknown.append(key)

    if unknown:
        suggestions: list[str] = []
        recognised_list = sorted(recognised)
        for bad in unknown:
            close = difflib.get_close_matches(bad.upper(), recognised_list, n=1, cutoff=0.4)
            suggestion = close[0] if close else "(no close match)"
            suggestions.append(f"  {bad!r}  →  closest Settings field: {suggestion}")
        detail = "\n".join(suggestions)
        pytest.fail(
            f"{source_label}: unrecognised env var(s):\n{detail}\n\n"
            f"If this is a container-infrastructure key (e.g. POSTGRES_*), "
            f"add it to _PASSTHROUGH_KEYS in tests/test_deploy_templates.py."
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_systemd_env_example_keys_are_recognised() -> None:
    """deploy/systemd/lmchat.env.example — all keys bind to Settings fields."""
    path = DEPLOY_DIR / "systemd" / "lmchat.env.example"
    assert path.exists(), f"Missing file: {path}"
    keys = _parse_env_file(path)
    _assert_all_recognised(keys, str(path.relative_to(REPO_ROOT)))


def test_env_local_example_keys_are_recognised() -> None:
    """<repo-root>/.env.local.example — all keys bind to Settings fields."""
    path = REPO_ROOT / ".env.local.example"
    assert path.exists(), f"Missing file: {path}"
    keys = _parse_env_file(path)
    _assert_all_recognised(keys, ".env.local.example")


def test_docker_compose_env_keys_are_recognised() -> None:
    """deploy/docker-compose.yml — all environment: keys bind to Settings fields."""
    path = DEPLOY_DIR / "docker-compose.yml"
    assert path.exists(), f"Missing file: {path}"
    keys = _parse_docker_compose_env(path)
    _assert_all_recognised(keys, str(path.relative_to(REPO_ROOT)))


def test_k8s_deployment_env_keys_are_recognised() -> None:
    """deploy/k8s/deployment.yaml — all env[].name entries bind to Settings fields."""
    path = DEPLOY_DIR / "k8s" / "deployment.yaml"
    assert path.exists(), f"Missing file: {path}"
    keys = _parse_k8s_deployment_env(path)
    _assert_all_recognised(keys, str(path.relative_to(REPO_ROOT)))


def test_env_local_example_covers_required_fields() -> None:
    """Smoke-check that .env.local.example covers the minimum required set.

    Verifies the three fields an admin MUST set before the app will boot
    are mentioned in the example file (either as active lines or in comments).
    """
    path = REPO_ROOT / ".env.local.example"
    content = path.read_text()
    for required_key in ("LM_CHAT_SECRET", "LM_STUDIO_BASE_URL", "LM_STUDIO_API_KEY"):
        assert required_key in content, (
            f".env.local.example does not mention {required_key!r}. "
            "Every Settings field (especially required ones) should be documented."
        )


def test_env_local_example_covers_all_settings_fields() -> None:
    """Every Settings field must appear in .env.local.example (active or commented-out).

    This is the inverse of test_env_local_example_keys_are_recognised: that test
    checks "every key in the file is a Settings field"; this test checks "every
    Settings field is in the file".  Together they prevent drift in either direction.
    """
    path = REPO_ROOT / ".env.local.example"
    assert path.exists(), f"Missing file: {path}"
    content = path.read_text()

    # Collect all keys mentioned in the file — including commented-out ones.
    # A commented key looks like:  # LM_CHAT_FOO=bar  or  #LM_CHAT_FOO=bar
    keys_in_file: set[str] = set()
    for raw_line in content.splitlines():
        stripped = raw_line.strip().lstrip("#").strip()
        if "=" in stripped:
            key, _, _ = stripped.partition("=")
            key = key.strip()
            if key:
                keys_in_file.add(key.upper())

    all_fields = _settings_field_names()
    missing = sorted(all_fields - keys_in_file)

    if missing:
        lines = "\n".join(f"  {f}" for f in missing)
        pytest.fail(
            f".env.local.example is missing {len(missing)} Settings field(s):\n{lines}\n\n"
            "Add a commented-out entry with the default value and a one-line description "
            "in the appropriate section."
        )
