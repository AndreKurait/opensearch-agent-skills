"""Shared fixtures for sync tests.

These tests exercise skills_sync.main end-to-end with real git commands
against synthetic local repositories in tmp_path. No network, no GitHub
API — the module-level path constants (REPO_ROOT, STATE_FILE, CACHE_DIR)
are monkeypatched to point into tmp_path.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


# Allow `import skills_sync` even when the package hasn't been installed
# (e.g. raw `pytest` invocation from the sync-bot/ directory). Normally
# `uv run pytest` handles this via the pyproject's `pythonpath = ["src"]`,
# but keeping the fallback makes ad-hoc invocation robust.
SYNC_BOT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = SYNC_BOT_ROOT / "src"
if SRC_DIR.exists() and str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def _git(cwd: Path, *args: str, env: dict | None = None) -> str:
    """Run a git command and return stdout (stripped). Raises on failure."""
    full_env = os.environ.copy()
    # Pin identity so test commits are deterministic and don't depend on
    # the developer's git config.
    full_env.update(
        {
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": "test-author@example.invalid",
            "GIT_COMMITTER_NAME": "Test Committer",
            "GIT_COMMITTER_EMAIL": "test-committer@example.invalid",
            # Force a stable default branch name across all git versions.
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        }
    )
    if env:
        full_env.update(env)
    r = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=full_env,
        check=True,
        text=True,
        capture_output=True,
    )
    return r.stdout.strip()


@pytest.fixture
def git_env():
    """Expose the helper so tests can run ad-hoc git commands with a stable identity."""
    return _git


@pytest.fixture
def make_upstream(tmp_path):
    """
    Factory that creates a bare-backed local "upstream" git repo under
    tmp_path/upstreams/<name>, returning its on-disk path.

    The returned repo is a normal (non-bare) working clone so the test
    can commit into it directly; sync.py will clone from this path via
    `file://` URL — the same fetch path used in production against
    github.com — giving us real end-to-end coverage of fetch/log/
    format-patch/am semantics without any network.
    """
    base = tmp_path / "upstreams"
    base.mkdir(exist_ok=True)

    def _factory(name: str) -> Path:
        repo = base / name
        repo.mkdir()
        _git(repo, "init", "-b", "main", ".")
        # First commit must exist so HEAD resolves; keep it empty of
        # tracked content the sync cares about.
        (repo / "README.md").write_text("# upstream\n")
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-m", "initial")
        return repo

    return _factory


@pytest.fixture
def commit_file():
    """
    Helper: write `content` to `repo/rel_path` and commit it with
    `message` under an optional custom author identity.
    Returns the new commit SHA.
    """

    def _commit(
        repo: Path,
        rel_path: str,
        content: str,
        message: str,
        *,
        author_name: str = "Upstream Dev",
        author_email: str = "upstream-dev@example.invalid",
    ) -> str:
        fp = repo / rel_path
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        _git(repo, "add", rel_path)
        _git(
            repo,
            "commit",
            "-m",
            message,
            env={
                "GIT_AUTHOR_NAME": author_name,
                "GIT_AUTHOR_EMAIL": author_email,
            },
        )
        return _git(repo, "rev-parse", "HEAD")

    return _commit


@pytest.fixture
def dest_repo(tmp_path):
    """A fresh local destination repo (simulates this repo in a PR branch)."""
    repo = tmp_path / "dest"
    repo.mkdir()
    _git(repo, "init", "-b", "main", ".")
    (repo / "README.md").write_text("# dest\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    return repo


@pytest.fixture
def sync_mod(tmp_path, dest_repo, monkeypatch):
    """
    Import skills_sync.main with its module-level REPO_ROOT / STATE_FILE /
    CACHE_DIR redirected into tmp_path. Any previously-imported copy is
    evicted so the constants are re-computed from scratch. Yields the
    module with an attached `.dest_repo` attribute for convenience.

    We import the concrete submodule (skills_sync.main) rather than the
    package so monkeypatches on constants like SYNC_BOT_NAME actually
    affect the bindings that the sync functions read at runtime (the
    package __init__ merely re-exports them, and patching the re-export
    has no effect on the originals).
    """
    # Drop any cached copies so re-patching REPO_ROOT / constants takes
    # effect when _resolve_repo_root() is re-evaluated on import.
    for mod in ("skills_sync", "skills_sync.main"):
        monkeypatch.delitem(sys.modules, mod, raising=False)

    import skills_sync.main as sync_module  # type: ignore

    # Redirect module-level paths into the test sandbox. We rebase to the
    # dest repo so state.json and .sync-cache live inside it — mirrors
    # production, where state.json is committed into this very repo.
    monkeypatch.setattr(sync_module, "REPO_ROOT", dest_repo, raising=True)
    monkeypatch.setattr(
        sync_module, "CACHE_DIR", dest_repo / ".sync-cache", raising=True
    )
    state_dir = dest_repo / "sync"
    state_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(
        sync_module, "STATE_FILE", state_dir / "state.json", raising=True
    )
    # Pin bot identity so signed-off-by trailers are deterministic.
    monkeypatch.setattr(sync_module, "SYNC_BOT_NAME", "test-bot", raising=True)
    monkeypatch.setattr(
        sync_module,
        "SYNC_BOT_EMAIL",
        "test-bot@example.invalid",
        raising=True,
    )
    sync_module.dest_repo = dest_repo  # type: ignore[attr-defined]
    return sync_module


def _as_file_url(path: Path) -> str:
    """file://-URL form that works on both Linux and macOS runners."""
    return path.resolve().as_uri()


@pytest.fixture
def file_url():
    return _as_file_url
