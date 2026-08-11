# SPDX-License-Identifier: Apache-2.0
"""§3.1.a — acceptance test for the commit-msg hygiene hook.

The hook lives at ``tools/git-hooks/commit-msg`` and is activated per
clone by ``make install-hooks``. It rejects any commit whose message
violates the LMChat hygiene rules
(no Co-Authored-By trailers, no AI-prose filler words, no emoji).

The test spins up a temp git repo, points ``core.hooksPath`` at the
real ``tools/git-hooks/`` tree (same wiring as ``make install-hooks``),
and asserts:

1. A clean message commits cleanly.
2. A message with ``Co-Authored-By:`` is rejected.
3. A message with an AI-prose filler (``leverages``) is rejected.
4. A message with an emoji glyph is rejected.
5. After every rejection, ``git log`` shows no new commit landed.

Word-boundary semantics matter — ``leverages`` rejects, ``leveraging``
allows.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOOKS_DIR = _REPO_ROOT / "tools" / "git-hooks"
_SCRIPT = _REPO_ROOT / "tools" / "check_commit_hygiene.py"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    assert _git(r, "init", "-q", "-b", "main").returncode == 0
    # Point core.hooksPath at the real on-disk tree — same wiring the
    # `make install-hooks` target uses. Using the real tree means a
    # drift between the test and the production hook is impossible.
    assert (
        _git(r, "config", "core.hooksPath", str(_HOOKS_DIR)).returncode == 0
    )
    (r / "a.txt").write_text("hello\n", encoding="utf-8")
    assert _git(r, "add", "a.txt").returncode == 0
    return r


def _head_count(r: Path) -> int:
    out = _git(r, "rev-list", "--count", "HEAD")
    if out.returncode != 0:
        return 0
    return int(out.stdout.strip() or "0")


def test_clean_message_commits(repo: Path) -> None:
    res = _git(repo, "commit", "-m", "feat: add hygiene check")
    assert res.returncode == 0, res.stderr
    assert _head_count(repo) == 1


def test_co_authored_by_rejected(repo: Path) -> None:
    res = _git(
        repo,
        "commit",
        "-m",
        "feat: add\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
    )
    assert res.returncode != 0
    assert "Co-Authored-By" in res.stderr or "AI-attribution" in res.stderr
    assert _head_count(repo) == 0


def test_ai_prose_filler_rejected(repo: Path) -> None:
    res = _git(
        repo,
        "commit",
        "-m",
        "feat: leverages the new RAG-mode resolver",
    )
    assert res.returncode != 0
    assert "AI-prose" in res.stderr or "leverages" in res.stderr.lower()
    assert _head_count(repo) == 0


def test_word_boundary_preserves_stems(repo: Path) -> None:
    # `leveraging` shares the stem but is not in the banned word list;
    # likewise `robustness`/`delivery` must pass.
    res = _git(
        repo,
        "commit",
        "-m",
        "feat: improve delivery and robustness while leveraging caches",
    )
    assert res.returncode == 0, res.stderr
    assert _head_count(repo) == 1


def test_emoji_rejected(repo: Path) -> None:
    res = _git(
        repo,
        "commit",
        "-m",
        "feat: add hygiene check \U0001F916",
    )
    assert res.returncode != 0
    assert "emoji" in res.stderr.lower()
    assert _head_count(repo) == 0


def test_check_head_target_runs(repo: Path) -> None:
    # First land a clean commit, then run `--check-head` against it —
    # this is the gates-target wire-up (catches a commit that slipped
    # through a clone without the hook).
    assert _git(repo, "commit", "-m", "feat: clean message").returncode == 0
    res = subprocess.run(
        ["python3", str(_SCRIPT), "--check-head"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
