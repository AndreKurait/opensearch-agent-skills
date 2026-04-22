#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""
Skill sync — import skill subdirectories from upstream repos into this one.

How it works
------------
For each entry in sync/sync.yaml:

  1. Shallow-ish fetch of the upstream repo into a cache dir under
     .sync-cache/<name> (persistent across runs for speed).
  2. Read the last-synced upstream commit SHA from sync/state.json for
     the (name, src_path, dest_path) tuple.
  3. Enumerate new upstream commits that touched `src_path`:
         git log --reverse <last>..<upstream_head> -- <src_path>
     First-ever run imports upstream history from the repo-root commit.
  4. For each such commit:
         a. git format-patch -1 --relative=<src_path> <sha>
            -> one patch with paths relative to src_path
         b. git am --directory=<dest_path> --keep-cr --committer-date-is-author-date
            -> applies under dest_path in this repo with ORIGINAL author preserved
         c. git commit --amend to append provenance trailers:
                 Source-Repo: <upstream url>
                 Source-Commit: <upstream sha>
                 Co-authored-by: <original author>      (already there via author field;
                                                         added explicitly so squashes keep it)
            and a Signed-off-by for the sync bot (committer).
  5. Update state.json with the new head SHA and commit it.

Push is handled by the caller (GitHub Actions workflow). This script only
modifies the working tree + index of the CURRENT repo via commits on HEAD.

Failure isolation
-----------------
Each source is wrapped in its own try/except. A failure during one source
resets that source's working tree (git am --abort, hard reset to the
pre-sync HEAD) and is recorded in the summary; other sources still run.
Exit code is nonzero iff at least one source failed.

Usage
-----
  uv run python sync/sync.py                     # sync all sources
  uv run python sync/sync.py --only NAME         # sync a single source
  uv run python sync/sync.py --dry-run           # show what would change
  uv run python sync/sync.py --config path.yaml  # custom config
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import yaml  # type: ignore

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / ".sync-cache"
STATE_FILE = REPO_ROOT / "sync" / "state.json"
DEFAULT_CONFIG = REPO_ROOT / "sync" / "sync.yaml"

SYNC_BOT_NAME = os.environ.get("SYNC_BOT_NAME", "opensearch-skills-sync[bot]")
SYNC_BOT_EMAIL = os.environ.get(
    "SYNC_BOT_EMAIL", "opensearch-skills-sync@users.noreply.github.com"
)


# ---------- helpers ----------

def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = True,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command, streaming or capturing output."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        capture_output=capture,
        env=full_env,
        input=input_text,
    )


def run_ok(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> bool:
    """Run a command; return True on exit 0."""
    r = run(cmd, cwd=cwd, check=False, env=env)
    return r.returncode == 0


def log(msg: str) -> None:
    print(f"[sync] {msg}", flush=True)


def ensure_git_identity(repo: Path) -> None:
    """Set the committer identity for the destination repo."""
    run(["git", "config", "user.name", SYNC_BOT_NAME], cwd=repo)
    run(["git", "config", "user.email", SYNC_BOT_EMAIL], cwd=repo)


# ---------- data model ----------

@dataclass
class Source:
    name: str
    url: str
    branch: str = "main"
    src_path: str = ""
    dest_path: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Source":
        missing = [k for k in ("name", "url", "src_path", "dest_path") if not d.get(k)]
        if missing:
            raise ValueError(f"source entry missing fields: {missing}: {d}")
        return cls(
            name=d["name"],
            url=d["url"],
            branch=d.get("branch", "main"),
            src_path=d["src_path"].strip("/"),
            dest_path=d["dest_path"].strip("/"),
        )

    @property
    def state_key(self) -> str:
        return f"{self.name}::{self.src_path}->{self.dest_path}"


@dataclass
class SourceResult:
    source: Source
    status: str  # "synced" | "up-to-date" | "failed" | "skipped"
    commits_imported: int = 0
    new_head: str = ""
    message: str = ""
    errors: list[str] = field(default_factory=list)


# ---------- state ----------

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"version": 1, "sources": {}}
    with STATE_FILE.open() as f:
        return json.load(f)


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.open("w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


# ---------- upstream cache ----------

def upstream_cache(source: Source) -> Path:
    # Use a name that's filesystem-safe
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", source.name)
    return CACHE_DIR / safe


def fetch_upstream(source: Source) -> tuple[Path, str]:
    """
    Clone or update the upstream repo in the cache.

    Returns (path-to-cache, head-SHA-of-branch).
    """
    cache = upstream_cache(source)
    cache.parent.mkdir(parents=True, exist_ok=True)

    if not (cache / ".git").exists():
        log(f"{source.name}: initial clone from {source.url} (branch {source.branch})")
        # Full clone (not --depth 1) because we may need history beyond the
        # last-synced SHA on a first run. filter=blob:none keeps it light.
        run(
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                "--branch",
                source.branch,
                source.url,
                str(cache),
            ]
        )
    else:
        log(f"{source.name}: fetching updates")
        # Ensure the origin URL is up to date in case config changed
        run(["git", "remote", "set-url", "origin", source.url], cwd=cache)
        run(
            [
                "git",
                "fetch",
                "--filter=blob:none",
                "origin",
                f"{source.branch}:refs/remotes/origin/{source.branch}",
            ],
            cwd=cache,
        )

    head = run(
        ["git", "rev-parse", f"refs/remotes/origin/{source.branch}"], cwd=cache
    ).stdout.strip()
    return cache, head


# ---------- core sync ----------

def commits_touching_path(
    cache: Path, src_path: str, since_sha: str | None, head_sha: str
) -> list[str]:
    """Return the list of commit SHAs (oldest first) that touched src_path."""
    rng = f"{since_sha}..{head_sha}" if since_sha else head_sha
    args = [
        "git",
        "log",
        "--reverse",
        "--no-merges",
        "--format=%H",
        rng,
        "--",
        src_path,
    ]
    r = run(args, cwd=cache)
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def import_commit(
    cache: Path,
    dest_repo: Path,
    source: Source,
    sha: str,
) -> str | None:
    """
    Import a single upstream commit into dest_repo under dest_path.

    Returns the new destination-commit SHA, or None if the patch produced
    no changes (e.g. a commit that only renamed files OUT of src_path).
    """
    # Get original metadata
    show = run(
        [
            "git",
            "show",
            "--no-patch",
            "--format=%H%x00%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%B",
            sha,
        ],
        cwd=cache,
    ).stdout
    parts = show.split("\x00", 7)
    if len(parts) < 8:
        raise RuntimeError(f"unexpected git show output for {sha}: {show!r}")
    _h, an, ae, adate, _cn, _ce, _cdate, body = parts
    orig_message = body.rstrip("\n")
    short = sha[:12]

    # Produce a patch rooted at src_path, then feed to `git am --directory=dest_path`.
    with tempfile.TemporaryDirectory(prefix=f"sync-patch-{short}-") as tmp:
        tmp_dir = Path(tmp)
        patch_args = [
            "git",
            "format-patch",
            "-1",
            "--stdout",
            # Keep binary patches working
            "--binary",
            # Paths in the patch become relative to src_path; empty src_path
            # means "repo root", handled naturally by not passing --relative.
        ]
        if source.src_path:
            patch_args.append(f"--relative={source.src_path}")
        patch_args.append(sha)

        patch = run(patch_args, cwd=cache).stdout
        if not patch.strip():
            return None

        patch_file = tmp_dir / f"{short}.patch"
        patch_file.write_text(patch)

        # Apply. Use --keep-non-patch and --keep-cr for robustness.
        am_env = {
            "GIT_COMMITTER_NAME": SYNC_BOT_NAME,
            "GIT_COMMITTER_EMAIL": SYNC_BOT_EMAIL,
        }
        am_args = [
            "git",
            "am",
            "--keep-cr",
            "--keep-non-patch",
            "--committer-date-is-author-date",
            f"--directory={source.dest_path}" if source.dest_path else "",
            str(patch_file),
        ]
        am_args = [a for a in am_args if a]
        r = run(am_args, cwd=dest_repo, check=False, env=am_env)

        if r.returncode != 0:
            # Detect "empty patch" failures (commit touched src_path only via
            # mode changes, or the whole patch cancels under relative path).
            combined = (r.stdout or "") + (r.stderr or "")
            if "Patch is empty" in combined or "patch does not apply" in combined:
                run(["git", "am", "--abort"], cwd=dest_repo, check=False)
                return None
            run(["git", "am", "--abort"], cwd=dest_repo, check=False)
            raise RuntimeError(
                f"git am failed for {source.name} commit {sha}:\n{combined}"
            )

    # Amend to append provenance trailers without touching tree.
    new_sha = run(["git", "rev-parse", "HEAD"], cwd=dest_repo).stdout.strip()
    trailers = [
        f"Source-Repo: {source.url}",
        f"Source-Commit: {sha}",
        # Author is already preserved by `git am`, but a Co-authored-by
        # trailer makes the attribution survive squash-merges and shows
        # the original author on the GitHub contributor graph for this repo.
        f"Co-authored-by: {an} <{ae}>",
        f"Signed-off-by: {SYNC_BOT_NAME} <{SYNC_BOT_EMAIL}>",
    ]
    # Use `git interpret-trailers --if-exists addIfDifferent` so re-runs are idempotent.
    new_message_proc = run(
        [
            "git",
            "interpret-trailers",
            "--if-exists",
            "addIfDifferent",
            "--trailer",
            trailers[0],
            "--trailer",
            trailers[1],
            "--trailer",
            trailers[2],
            "--trailer",
            trailers[3],
        ],
        cwd=dest_repo,
        input_text=orig_message + "\n",
    )
    new_message = new_message_proc.stdout

    run(
        ["git", "commit", "--amend", "-m", new_message, "--no-edit",
         # keep author as set by `git am`
         ],
        cwd=dest_repo,
        env={
            "GIT_COMMITTER_NAME": SYNC_BOT_NAME,
            "GIT_COMMITTER_EMAIL": SYNC_BOT_EMAIL,
        },
    )
    return run(["git", "rev-parse", "HEAD"], cwd=dest_repo).stdout.strip()


def sync_one(source: Source, state: dict, dest_repo: Path, dry_run: bool) -> SourceResult:
    result = SourceResult(source=source, status="up-to-date")
    try:
        cache, head_sha = fetch_upstream(source)
        last = state["sources"].get(source.state_key, {}).get("last_sha")

        if last == head_sha:
            result.message = f"already at {head_sha[:12]}"
            return result

        shas = commits_touching_path(cache, source.src_path, last, head_sha)
        if not shas:
            # No commits touched this path in the range, but upstream head
            # advanced. Still bump state so we don't re-scan forever.
            result.status = "up-to-date"
            result.new_head = head_sha
            result.message = (
                f"no commits touched {source.src_path} in "
                f"{(last or 'INIT')[:12]}..{head_sha[:12]}"
            )
            if not dry_run:
                state["sources"].setdefault(source.state_key, {}).update(
                    {
                        "last_sha": head_sha,
                        "url": source.url,
                        "branch": source.branch,
                        "src_path": source.src_path,
                        "dest_path": source.dest_path,
                    }
                )
            return result

        log(
            f"{source.name}: {len(shas)} upstream commit(s) to import "
            f"(from {(last or 'INIT')[:12]} to {head_sha[:12]})"
        )
        if dry_run:
            for sha in shas:
                subj = run(
                    ["git", "log", "-1", "--format=%s", sha], cwd=cache
                ).stdout.strip()
                log(f"  would import {sha[:12]}  {subj}")
            result.status = "synced"
            result.commits_imported = len(shas)
            result.new_head = head_sha
            return result

        ensure_git_identity(dest_repo)
        pre_head = run(["git", "rev-parse", "HEAD"], cwd=dest_repo).stdout.strip()

        imported = 0
        for sha in shas:
            new_sha = import_commit(cache, dest_repo, source, sha)
            if new_sha is None:
                log(f"  skipped (empty under relative path): {sha[:12]}")
                continue
            imported += 1
            log(f"  imported {sha[:12]} -> {new_sha[:12]}")

        # Always update state.json, even if every imported commit ended up empty —
        # otherwise we'd rescan them next run.
        state["sources"].setdefault(source.state_key, {}).update(
            {
                "last_sha": head_sha,
                "url": source.url,
                "branch": source.branch,
                "src_path": source.src_path,
                "dest_path": source.dest_path,
            }
        )
        save_state(state)
        # Commit the state bump (on top of the imported commits, or as its
        # own commit if nothing was imported). Use a simple identity-signed
        # sync commit.
        run(["git", "add", str(STATE_FILE.relative_to(REPO_ROOT))], cwd=dest_repo)
        if run_ok(["git", "diff", "--cached", "--quiet"], cwd=dest_repo):
            # no state change (first-run identical) -- nothing to commit
            pass
        else:
            msg = (
                f"chore(sync): advance {source.name} state to {head_sha[:12]}\n\n"
                f"Source-Repo: {source.url}\n"
                f"Source-Branch: {source.branch}\n"
                f"Source-Commit: {sha}\n"
                f"Signed-off-by: {SYNC_BOT_NAME} <{SYNC_BOT_EMAIL}>\n"
            )
            run(
                ["git", "commit", "-m", msg],
                cwd=dest_repo,
                env={
                    "GIT_COMMITTER_NAME": SYNC_BOT_NAME,
                    "GIT_COMMITTER_EMAIL": SYNC_BOT_EMAIL,
                    "GIT_AUTHOR_NAME": SYNC_BOT_NAME,
                    "GIT_AUTHOR_EMAIL": SYNC_BOT_EMAIL,
                },
            )

        result.status = "synced" if imported > 0 else "up-to-date"
        result.commits_imported = imported
        result.new_head = head_sha
        result.message = (
            f"imported {imported}/{len(shas)} commits "
            f"(upstream at {head_sha[:12]})"
        )
        return result
    except Exception as e:  # noqa: BLE001
        # Abort any in-progress am, hard reset to pre-sync HEAD to keep
        # other sources unaffected.
        run(["git", "am", "--abort"], cwd=dest_repo, check=False)
        if "pre_head" in locals():
            run(["git", "reset", "--hard", pre_head], cwd=dest_repo, check=False)
        result.status = "failed"
        result.message = str(e)
        result.errors.append(str(e))
        log(f"{source.name}: FAILED -> {e}")
        return result


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--only", type=str, default=None, help="sync only this source name")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg_path: Path = args.config
    if not cfg_path.exists():
        log(f"config not found: {cfg_path}")
        return 2

    with cfg_path.open() as f:
        cfg = yaml.safe_load(f)

    sources_raw = cfg.get("sources") or []
    if not sources_raw:
        log("no sources defined, nothing to do")
        return 0

    sources = [Source.from_dict(s) for s in sources_raw]
    if args.only:
        sources = [s for s in sources if s.name == args.only]
        if not sources:
            log(f"no source named {args.only!r}")
            return 2

    state = load_state()
    results: list[SourceResult] = []
    for s in sources:
        log(f"--- syncing: {s.name} ({s.src_path} -> {s.dest_path}) ---")
        results.append(sync_one(s, state, REPO_ROOT, args.dry_run))

    # Persist state one more time at the end (covers the no-op branches).
    if not args.dry_run:
        save_state(state)

    # Summary
    print()
    log("=== SUMMARY ===")
    exit_code = 0
    for r in results:
        line = f"  {r.source.name:40s}  {r.status:12s}  {r.message}"
        log(line)
        if r.status == "failed":
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
