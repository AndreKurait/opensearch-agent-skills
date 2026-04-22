#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "skills-ref>=0.1.1"]
# ///
"""
Skill sync — import skill subdirectories from upstream repos into this one.

How it works
------------
For each source file in sync/sources/*.yaml:

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
  uv run python sync/sync.py                          # sync all sources
  uv run python sync/sync.py --only NAME              # sync a single source
  uv run python sync/sync.py --dry-run                # show what would change
  uv run python sync/sync.py --sources-dir path/      # custom sources dir
"""

from __future__ import annotations

import argparse
import hashlib
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
DEFAULT_SOURCES_DIR = REPO_ROOT / "sync" / "sources"

# Default identity: opensearch-ci-bot (GitHub user id 83309141, display name
# "opensearch-ci"). The noreply `<id>+<login>@users.noreply.github.com` form
# makes GitHub auto-resolve the commit author to that profile, so every sync
# commit renders with the opensearch-ci-bot avatar + clickable profile link
# on github.com — without needing to register a GitHub App or hold a PAT for
# that account. Override via SYNC_BOT_NAME / SYNC_BOT_EMAIL env for local runs.
SYNC_BOT_NAME = os.environ.get("SYNC_BOT_NAME", "opensearch-ci")
SYNC_BOT_EMAIL = os.environ.get(
    "SYNC_BOT_EMAIL", "83309141+opensearch-ci-bot@users.noreply.github.com"
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


def prefix_subject(message: str, source_name: str) -> str:
    """
    Return `message` with an LLVM-style `[source_name] ` prefix on the
    subject line, unless it already starts with that exact prefix
    (making re-runs idempotent).

    Only the subject (first line) is rewritten; body + trailers are left
    intact. An empty message is returned as-is — `git am` already rejects
    those upstream.
    """
    if not message:
        return message
    prefix = f"[{source_name}] "
    newline, _, rest = message.partition("\n")
    subject = newline
    if subject.startswith(prefix):
        return message
    new_subject = prefix + subject
    if rest == "" and "\n" not in message:
        return new_subject
    return new_subject + "\n" + rest


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
        input_text=prefix_subject(orig_message, source.name) + "\n",
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


# ---------- skill spec validation ----------
#
# After a source's commits have been imported, we run the Agent Skills Spec
# validator (github.com/agentskills/agentskills, Apache-2.0) against every
# SKILL.md under the source's dest_path. Any spec violation -- wrong YAML
# shape, illegal top-level field, name mismatch, missing required field --
# aborts the whole source import. The existing sync_one except block then
# hard-resets to pre_head and the issue reporter files a failure issue,
# so callers see the same visibility as any other sync failure.
#
# We validate the WHOLE dest tree, not just the individual files each
# commit touched. A commit that didn't touch SKILL.md can still invalidate
# an existing skill (e.g. renaming the directory without updating `name`),
# and re-validating the whole tree on every successful import gives us a
# cheap invariant: "if sync succeeded, every skill under dest_path is
# spec-valid at HEAD." That's what downstream consumers actually care about.

def validate_skill_tree(tree_root: Path) -> dict[Path, list[str]]:
    """Validate every skill under `tree_root` against the Agent Skills Spec.

    Returns a mapping of skill_dir -> list[error_msg] for skills that failed
    validation. Empty dict means everything under this tree is spec-valid.

    A "skill" is any directory that contains a SKILL.md file. The parent
    of each SKILL.md is passed to skills_ref.validator.validate(), which
    checks frontmatter fields, name<->directory match, YAML block style,
    allowed fields, length limits, etc.
    """
    # Import lazily: keeps the top-of-file import block lean and means a
    # missing skills-ref install (e.g. someone running an old checkout
    # without `uv sync`) surfaces a clear error only if we actually try
    # to validate, not at module load time.
    from skills_ref.validator import validate as sr_validate

    failures: dict[Path, list[str]] = {}
    if not tree_root.exists():
        # No skills imported yet (e.g. first run against an empty upstream).
        # Nothing to validate -- that's not an error.
        return failures
    for skill_md in sorted(tree_root.rglob("SKILL.md")):
        skill_dir = skill_md.parent
        errors = sr_validate(skill_dir)
        if errors:
            failures[skill_dir] = errors
    return failures


def format_validation_failures(
    tree_root: Path, failures: dict[Path, list[str]]
) -> str:
    """Render validation failures as a human-readable block for issue bodies."""
    lines = [
        f"Agent Skills Spec validation failed for {len(failures)} skill(s) "
        f"under `{tree_root}`:",
        "",
    ]
    for skill_dir, errors in sorted(failures.items()):
        rel = skill_dir.relative_to(REPO_ROOT) if skill_dir.is_absolute() else skill_dir
        lines.append(f"- `{rel}/SKILL.md`")
        for err in errors:
            lines.append(f"    - {err}")
    lines += [
        "",
        "Fix the SKILL.md files in the upstream repo so they comply with the "
        "spec at https://agentskills.io. Imports for this source are rolled "
        "back; the next sync run after the upstream is fixed will retry.",
    ]
    return "\n".join(lines)


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

        # Validate the post-import dest tree against the Agent Skills Spec.
        # Any spec violation raises, which the outer `except` turns into a
        # hard reset to pre_head + a failure result -> issue opened.
        # We do this BEFORE bumping state.json so a failed validation also
        # leaves last_sha unchanged, so the next run retries the same range
        # rather than silently skipping past a broken commit.
        dest_tree = dest_repo / source.dest_path
        validation_failures = validate_skill_tree(dest_tree)
        if validation_failures:
            raise RuntimeError(
                format_validation_failures(dest_tree, validation_failures)
            )

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
                f"[{source.name}] chore(sync): advance state to {head_sha[:12]}\n\n"
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


# ---------- github failure reporter ----------
#
# On failure we surface the failure somewhere a human will see it, but we
# pick the reporting channel based on where the run happened:
#
#   * Default branch (scheduled / dispatch on main): open/update a GitHub
#     issue in the destination repo. Issues are persistent, labelled, and
#     de-duped — perfect for cron failures nobody is watching.
#
#   * Non-default branch (dispatch on a feature branch): if an open PR
#     exists for that branch, post a comment on the PR. Otherwise stay
#     silent and rely on the failed workflow run itself as the signal.
#     We explicitly avoid opening issues for PR-branch runs because they
#     would clutter the tracker with transient failures that belong to an
#     in-flight change.
#
# Issue-flow design (default branch):
#
#   - Primary dedupe key is the pair of labels (sync-failure, sync-source:<name>).
#     A single open issue per failing source. Multiple source failures ->
#     multiple issues. An issue is auto-created the first time a source
#     fails and stays open across subsequent failures of the same source.
#
#   - The current error hash is stored in the issue body as an HTML comment
#     marker. On re-failure with the SAME error, we stay quiet (no new
#     comment) — the open issue itself is the signal. On re-failure with a
#     DIFFERENT error, we post a comment with the new error details and
#     update the marker. Prevents spam when a source is stuck in the same
#     mode, still surfaces meaningfully changed failures.
#
#   - When a previously-failing source comes back up-to-date or syncs
#     successfully, we close all matching open issues with a comment.
#
# PR-comment flow (non-default branch):
#
#   - Find the open PR whose HEAD ref matches the current branch. If none,
#     no-op (the failed action status is the signal).
#   - Dedupe by the same <!-- sync-error-hash: ... --> marker embedded in
#     bot comments on the PR. If the most recent sync-failure comment has
#     the same hash as the current error, stay silent — the existing
#     comment already describes this failure. Otherwise post a new comment.
#   - On success/recovery we do NOT delete or resolve prior PR comments.
#     They remain as a record of prior attempts; the green run is the
#     recovery signal for PR authors.
#
#   - All network + gh calls are wrapped so issue-tracker failures never
#     affect the sync exit code. If gh is unavailable or unauthenticated
#     (e.g. local dev), the reporter logs a skip line and returns.
#
# We shell out to `gh` rather than hitting the REST API directly because
# it's already on GitHub-hosted runners and handles auth from GITHUB_TOKEN
# automatically via the GH_TOKEN env var set in the workflow.

SYNC_FAILURE_LABEL = "sync-failure"
SOURCE_LABEL_PREFIX = "sync-source:"
ERROR_HASH_MARKER_RE = re.compile(r"<!-- sync-error-hash: ([0-9a-f]{12}) -->")


def _gh_available(repo: str) -> bool:
    """Check gh CLI is installed and authed for the target repo."""
    if not shutil.which("gh"):
        return False
    # `gh auth status` exits 0 if authed. We pass --hostname to be explicit.
    r = run(["gh", "auth", "status"], check=False)
    if r.returncode != 0:
        return False
    # Verify we can talk to the repo (catches missing scope / wrong token).
    r = run(["gh", "repo", "view", repo, "--json", "name"], check=False)
    return r.returncode == 0


def _error_hash(source_name: str, err: str) -> str:
    """
    Hash a normalized form of the error so that "same class of failure"
    produces the same hash across runs. We strip volatile bits:
      - absolute paths under /home, /tmp, /runner, /github (CI artifacts)
      - 7-40 hex SHAs
      - ISO-ish timestamps and epoch seconds
      - line/column numbers in tracebacks
    """
    norm = err
    norm = re.sub(r"/(home|tmp|runner|github|Users|var)/[^\s'\"]+", "<path>", norm)
    norm = re.sub(r"\b[0-9a-f]{7,40}\b", "<sha>", norm)
    norm = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^ ]*", "<ts>", norm)
    norm = re.sub(r'line \d+', "line <n>", norm)
    h = hashlib.sha256(f"{source_name}\0{norm}".encode()).hexdigest()
    return h[:12]


def _find_open_issue(repo: str, source_name: str) -> dict | None:
    """
    Return the single open issue for this source, or None. If multiple are
    open (shouldn't happen, but be defensive), return the newest.
    """
    label_args = [
        "--label", SYNC_FAILURE_LABEL,
        "--label", f"{SOURCE_LABEL_PREFIX}{source_name}",
    ]
    r = run(
        ["gh", "issue", "list", "--repo", repo, "--state", "open",
         *label_args, "--json", "number,title,body,updatedAt", "--limit", "10"],
        check=False,
    )
    if r.returncode != 0:
        log(f"issue-reporter: gh issue list failed: {r.stderr.strip()}")
        return None
    try:
        issues = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not issues:
        return None
    # Newest first by updatedAt
    issues.sort(key=lambda i: i.get("updatedAt", ""), reverse=True)
    return issues[0]


def _ensure_labels(repo: str, names: Iterable[str]) -> None:
    """
    Create any missing labels. `gh label create` is idempotent-ish — it
    errors if the label exists, so we suppress that. Safe to call every run.
    """
    for name in names:
        color = "B60205" if name == SYNC_FAILURE_LABEL else "D4C5F9"
        run(
            ["gh", "label", "create", name, "--repo", repo,
             "--color", color, "--description", f"Auto-managed by sync/sync.py"],
            check=False,
        )


def _format_issue_body(source: Source, err: str, err_hash: str, run_url: str | None) -> str:
    # Keep the error payload bounded — very long tracebacks get truncated
    # (still recorded in full in the Actions log).
    MAX_ERR = 6000
    err_trimmed = err if len(err) <= MAX_ERR else err[:MAX_ERR] + "\n…[truncated]"
    lines = [
        f"The skill-sync job failed while mirroring `{source.name}`.",
        "",
        f"- **Upstream:** {source.url} (branch `{source.branch}`)",
        f"- **Upstream path:** `{source.src_path}`",
        f"- **Destination:** `{source.dest_path}`",
    ]
    if run_url:
        lines.append(f"- **Workflow run:** {run_url}")
    lines += [
        "",
        "## Error",
        "",
        "```",
        err_trimmed,
        "```",
        "",
        "---",
        "",
        "This issue is auto-managed by `sync/sync.py`. It will be closed "
        "automatically the next time this source syncs successfully. If the "
        "error signature changes, a new comment will be posted here.",
        "",
        f"<!-- sync-error-hash: {err_hash} -->",
    ]
    return "\n".join(lines)


def _open_or_update_failure_issue(
    repo: str, source: Source, err: str, run_url: str | None
) -> None:
    err_hash = _error_hash(source.name, err)
    existing = _find_open_issue(repo, source.name)

    if existing is None:
        # First failure for this source (or any prior issues were closed).
        _ensure_labels(repo, [SYNC_FAILURE_LABEL, f"{SOURCE_LABEL_PREFIX}{source.name}"])
        body = _format_issue_body(source, err, err_hash, run_url)
        title = f"[sync] {source.name}: mirror failed"
        r = run(
            ["gh", "issue", "create", "--repo", repo,
             "--title", title, "--body", body,
             "--label", SYNC_FAILURE_LABEL,
             "--label", f"{SOURCE_LABEL_PREFIX}{source.name}"],
            check=False,
        )
        if r.returncode == 0:
            log(f"issue-reporter: opened issue for {source.name}: {r.stdout.strip()}")
        else:
            log(f"issue-reporter: failed to open issue for {source.name}: {r.stderr.strip()}")
        return

    # An open issue exists. Decide whether to stay silent or post an update.
    number = existing["number"]
    prior_hash_match = ERROR_HASH_MARKER_RE.search(existing.get("body") or "")
    prior_hash = prior_hash_match.group(1) if prior_hash_match else None

    if prior_hash == err_hash:
        log(f"issue-reporter: #{number} already tracks this failure mode for "
            f"{source.name} (hash {err_hash}); no comment posted")
        return

    # Error changed — update the issue body's hash marker and post a
    # comment summarizing the new failure signature.
    old_body = existing.get("body") or ""
    if prior_hash_match:
        new_body = ERROR_HASH_MARKER_RE.sub(f"<!-- sync-error-hash: {err_hash} -->", old_body)
    else:
        new_body = old_body.rstrip() + f"\n\n<!-- sync-error-hash: {err_hash} -->\n"
    run(
        ["gh", "issue", "edit", str(number), "--repo", repo, "--body", new_body],
        check=False,
    )

    MAX_ERR = 6000
    err_trimmed = err if len(err) <= MAX_ERR else err[:MAX_ERR] + "\n…[truncated]"
    comment_lines = ["The failure signature changed."]
    if run_url:
        comment_lines.append(f"Workflow run: {run_url}")
    comment_lines += ["", "```", err_trimmed, "```"]
    comment = "\n".join(comment_lines)
    r = run(
        ["gh", "issue", "comment", str(number), "--repo", repo, "--body", comment],
        check=False,
    )
    if r.returncode == 0:
        log(f"issue-reporter: updated #{number} for {source.name} (new hash {err_hash})")
    else:
        log(f"issue-reporter: failed to comment on #{number}: {r.stderr.strip()}")


def _close_recovered_issue(repo: str, source: Source, run_url: str | None) -> None:
    existing = _find_open_issue(repo, source.name)
    if existing is None:
        return
    number = existing["number"]
    body_lines = ["Sync recovered — closing automatically."]
    if run_url:
        body_lines.append(f"Workflow run: {run_url}")
    r = run(
        ["gh", "issue", "close", str(number), "--repo", repo,
         "--comment", "\n".join(body_lines)],
        check=False,
    )
    if r.returncode == 0:
        log(f"issue-reporter: closed #{number} — {source.name} recovered")
    else:
        log(f"issue-reporter: failed to close #{number}: {r.stderr.strip()}")


def _all_open_failure_issues(repo: str) -> list[dict]:
    """Return every open sync-failure issue on the repo (all sources)."""
    r = run(
        ["gh", "issue", "list", "--repo", repo, "--state", "open",
         "--label", SYNC_FAILURE_LABEL,
         "--json", "number,title,labels,body,updatedAt", "--limit", "100"],
        check=False,
    )
    if r.returncode != 0:
        log(f"issue-reporter: gh issue list (all) failed: {r.stderr.strip()}")
        return []
    try:
        return json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return []


def _source_name_from_issue(issue: dict) -> str | None:
    for lbl in issue.get("labels") or []:
        name = lbl.get("name", "")
        if name.startswith(SOURCE_LABEL_PREFIX):
            return name[len(SOURCE_LABEL_PREFIX):]
    return None


def _is_default_branch(repo: str) -> bool:
    """
    Return True iff the current GITHUB_REF_NAME matches the repo's default
    branch. Conservative: if we can't determine either side, return False
    (safer to post a PR comment or stay silent than to cut an issue).
    """
    ref_name = os.environ.get("GITHUB_REF_NAME")
    if not ref_name:
        return False
    r = run(
        ["gh", "repo", "view", repo, "--json", "defaultBranchRef"],
        check=False,
    )
    if r.returncode != 0:
        log(f"issue-reporter: gh repo view failed ({r.stderr.strip()}); "
            "assuming non-default branch")
        return False
    try:
        info = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return False
    default = (info.get("defaultBranchRef") or {}).get("name")
    return bool(default) and ref_name == default


def _find_pr_for_ref(repo: str, ref_name: str) -> int | None:
    """
    Return the number of the (single) open PR whose head branch matches
    ref_name, or None. Multiple-PR case is rare; we return the newest.
    """
    r = run(
        ["gh", "pr", "list", "--repo", repo, "--state", "open",
         "--head", ref_name, "--json", "number,updatedAt", "--limit", "5"],
        check=False,
    )
    if r.returncode != 0:
        log(f"issue-reporter: gh pr list failed: {r.stderr.strip()}")
        return None
    try:
        prs = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not prs:
        return None
    prs.sort(key=lambda p: p.get("updatedAt", ""), reverse=True)
    return prs[0].get("number")


# A signature line we embed in every PR failure comment so we can
# distinguish our comments from anyone else's when de-duping.
PR_COMMENT_SIGNATURE = "<!-- sync-failure-comment -->"


def _post_or_update_pr_comment(
    repo: str, pr_number: int, source: "Source", err: str, run_url: str | None,
) -> None:
    """
    Post a failure comment on the given PR, de-duplicated by error-hash.

    We scan existing comments on the PR for any that carry our signature
    marker AND a source-label marker for this source. If the most-recent
    such comment's error-hash matches the current error, stay silent. Any
    other case posts a fresh comment — we never edit old comments so the
    chronological history of failure signatures is preserved on the PR.
    """
    err_hash = _error_hash(source.name, err)

    # List recent comments and look for prior sync-failure comments for
    # this source. `gh pr view --json comments` gives us the bodies.
    r = run(
        ["gh", "pr", "view", str(pr_number), "--repo", repo,
         "--json", "comments"],
        check=False,
    )
    prior_hash: str | None = None
    if r.returncode == 0:
        try:
            data = json.loads(r.stdout or "{}")
        except json.JSONDecodeError:
            data = {}
        comments = data.get("comments") or []
        source_marker = f"<!-- sync-source: {source.name} -->"
        # Comments come back oldest-first; walk in reverse to find the
        # most recent matching one.
        for c in reversed(comments):
            body = c.get("body") or ""
            if PR_COMMENT_SIGNATURE in body and source_marker in body:
                m = ERROR_HASH_MARKER_RE.search(body)
                prior_hash = m.group(1) if m else None
                break
    else:
        log(f"issue-reporter: gh pr view failed: {r.stderr.strip()}")

    if prior_hash == err_hash:
        log(f"issue-reporter: PR #{pr_number} already has a comment for "
            f"{source.name} with the same failure signature (hash "
            f"{err_hash}); no comment posted")
        return

    MAX_ERR = 6000
    err_trimmed = err if len(err) <= MAX_ERR else err[:MAX_ERR] + "\n…[truncated]"
    lines = [
        PR_COMMENT_SIGNATURE,
        f"<!-- sync-source: {source.name} -->",
        f"Skill sync failed for **`{source.name}`** on this branch.",
        "",
        f"- **Upstream:** {source.url} (branch `{source.branch}`)",
        f"- **Upstream path:** `{source.src_path}`",
        f"- **Destination:** `{source.dest_path}`",
    ]
    if run_url:
        lines.append(f"- **Workflow run:** {run_url}")
    lines += [
        "",
        "<details><summary>Error</summary>",
        "",
        "```",
        err_trimmed,
        "```",
        "",
        "</details>",
        "",
        "_This comment is auto-posted by `sync/sync.py` when the sync job "
        "runs on a non-default branch. No GitHub issue will be opened for "
        "PR-branch failures._",
        "",
        f"<!-- sync-error-hash: {err_hash} -->",
    ]
    body = "\n".join(lines)
    rc = run(
        ["gh", "pr", "comment", str(pr_number), "--repo", repo, "--body", body],
        check=False,
    )
    if rc.returncode == 0:
        log(f"issue-reporter: commented on PR #{pr_number} for "
            f"{source.name} (hash {err_hash})")
    else:
        log(f"issue-reporter: failed to comment on PR #{pr_number}: "
            f"{rc.stderr.strip()}")


def report_results_to_github(results: list[SourceResult]) -> None:
    """
    Surface sync outcomes in GitHub. The reporting channel depends on which
    branch the workflow is running on:

      * Default branch  -> open/update/close labelled failure issues.
      * Non-default ref -> post (de-duplicated) comments on the PR whose
                           HEAD matches the current ref, if one exists.
                           No issues are opened from non-default branches.

    Best-effort in every case: never affects the sync exit code, silently
    skips in non-GitHub contexts, and swallows all gh errors.
    """
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        log("issue-reporter: GITHUB_REPOSITORY not set; skipping")
        return
    try:
        if not _gh_available(repo):
            log(f"issue-reporter: gh unavailable or not authed for {repo}; skipping")
            return
    except Exception as e:  # pragma: no cover - defensive
        log(f"issue-reporter: gh availability check errored: {e}; skipping")
        return

    # Build a link to the current workflow run if we're in CI.
    run_url: str | None = None
    server = os.environ.get("GITHUB_SERVER_URL")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if server and run_id:
        run_url = f"{server}/{repo}/actions/runs/{run_id}"

    on_default = _is_default_branch(repo)
    ref_name = os.environ.get("GITHUB_REF_NAME", "")
    if not on_default:
        log(
            f"issue-reporter: running on non-default branch {ref_name!r}; "
            "will route failures to PR comments instead of issues"
        )
        pr_number = _find_pr_for_ref(repo, ref_name) if ref_name else None
        for r in results:
            if r.status != "failed":
                continue
            if pr_number is None:
                log(
                    f"issue-reporter: {r.source.name} failed on {ref_name!r} "
                    "but no open PR was found; relying on failed workflow "
                    "status as the signal"
                )
                continue
            err_text = "\n".join(r.errors) if r.errors else (r.message or "unknown error")
            try:
                _post_or_update_pr_comment(repo, pr_number, r.source, err_text, run_url)
            except Exception as e:  # pragma: no cover
                log(f"issue-reporter: unexpected error commenting on PR #{pr_number} for {r.source.name}: {e}")
        return

    # Default branch: full issue-tracker flow (open/update/close).
    for r in results:
        try:
            if r.status == "failed":
                err_text = "\n".join(r.errors) if r.errors else (r.message or "unknown error")
                _open_or_update_failure_issue(repo, r.source, err_text, run_url)
            elif r.status in ("synced", "up-to-date"):
                _close_recovered_issue(repo, r.source, run_url)
            # status == "skipped" -> do nothing
        except Exception as e:  # pragma: no cover
            log(f"issue-reporter: unexpected error handling {r.source.name}: {e}")

    # Close issues for sources that no longer exist in the config. When a
    # user removes a bad source, sync_one is never called for it — so we'd
    # otherwise leave the issue open forever. Skip this sweep when only a
    # subset of sources ran (e.g. workflow_dispatch with `--only`), since
    # we can't distinguish "removed from config" from "not in this run".
    if os.environ.get("SYNC_FULL_RUN") == "1":
        seen_source_names = {r.source.name for r in results}
        for issue in _all_open_failure_issues(repo):
            src_name = _source_name_from_issue(issue)
            if not src_name or src_name in seen_source_names:
                continue
            number = issue["number"]
            comment = (
                "Source was removed from the sync configuration — closing "
                "automatically."
            )
            if run_url:
                comment += f"\n\nWorkflow run: {run_url}"
            rc = run(
                ["gh", "issue", "close", str(number), "--repo", repo,
                 "--comment", comment],
                check=False,
            )
            if rc.returncode == 0:
                log(f"issue-reporter: closed #{number} — source {src_name!r} removed from config")
            else:
                log(f"issue-reporter: failed to close #{number} for removed source {src_name!r}: {rc.stderr.strip()}")


def load_sources_from_dir(sources_dir: Path) -> list["Source"]:
    """
    Load sources from `sources_dir` — one YAML file per source.

    Each file must be a mapping with at least `name`, `url`, `src_path`,
    `dest_path` at the top level. `sources:` list-of-entries files are
    also accepted for back-compat and for the rare case where a file
    defines multiple sources that share context.

    Files are loaded in lexicographic order so sync order is deterministic
    and reviewable. Hidden files and non-`.yaml` / non-`.yml` files are
    ignored. Duplicate source names across files are a hard error.
    """
    if not sources_dir.exists() or not sources_dir.is_dir():
        raise FileNotFoundError(f"sources directory not found: {sources_dir}")

    files = sorted(
        p for p in sources_dir.iterdir()
        if p.is_file() and p.suffix in (".yaml", ".yml") and not p.name.startswith(".")
    )

    sources: list[Source] = []
    seen: dict[str, Path] = {}
    for path in files:
        with path.open() as f:
            doc = yaml.safe_load(f)
        if doc is None:
            continue
        if isinstance(doc, dict) and "sources" in doc:
            entries = doc.get("sources") or []
        elif isinstance(doc, dict):
            entries = [doc]
        else:
            raise ValueError(
                f"{path}: top-level must be a mapping "
                f"(single source) or {{sources: [...]}}"
            )
        for entry in entries:
            src = Source.from_dict(entry)
            if src.name in seen:
                raise ValueError(
                    f"duplicate source name {src.name!r}: "
                    f"defined in both {seen[src.name]} and {path}"
                )
            seen[src.name] = path
            sources.append(src)
    return sources


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--sources-dir",
        type=Path,
        default=DEFAULT_SOURCES_DIR,
        help="directory containing one YAML file per source (default: sync/sources/)",
    )
    ap.add_argument("--only", type=str, default=None, help="sync only this source name")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sources_dir: Path = args.sources_dir
    try:
        sources = load_sources_from_dir(sources_dir)
    except FileNotFoundError as e:
        log(str(e))
        return 2
    except ValueError as e:
        log(f"config error: {e}")
        return 2

    if not sources:
        log(f"no sources defined under {sources_dir}, nothing to do")
        return 0

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

    # Open/update/close GitHub issues based on results. Best-effort:
    # never affects exit code, silently skips in non-GitHub contexts.
    # Disabled under --dry-run so local rehearsals don't touch the tracker.
    if not args.dry_run:
        # Signal to the reporter that this run processed every configured
        # source, so it's safe to close issues for sources no longer in
        # the config. Under --only we processed a subset, so skip that sweep.
        if not args.only:
            os.environ["SYNC_FULL_RUN"] = "1"
        try:
            report_results_to_github(results)
        except Exception as e:  # pragma: no cover - absolute belt-and-suspenders
            log(f"issue-reporter: top-level error swallowed: {e}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
