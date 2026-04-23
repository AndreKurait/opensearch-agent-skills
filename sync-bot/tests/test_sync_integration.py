"""
Integration tests for sync/sync.py against synthetic local git repos.

Each test builds a miniature "upstream" repo in tmp_path, points a
Source at it via a file:// URL, and runs `sync_one` against a fresh
destination repo. This exercises the real fetch / git log /
format-patch / git am pipeline — the same code path that runs against
GitHub in production — but is offline, deterministic, and takes
milliseconds per test.

Key invariants we lock in:

  - first-ever sync imports the full history of src_path,
  - re-run with no upstream changes is a no-op (idempotent),
  - upstream commits that do NOT touch src_path leave state.json
    unchanged,   <-- this is the scenario that motivated PR #12
  - author attribution is preserved,
  - [source_name] subject prefix is applied,
  - provenance trailers (Source-Repo / Source-Commit) and Signed-off-by
    are appended,
  - validation failure hard-resets and does not bump state.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


SKILL_FRONTMATTER = """\
---
name: {name}
description: A test skill used by the sync integration test suite to exercise git-am import, subject prefixing, and provenance trailers.
---

# {name}

Body for the {name} skill.
"""


def _skill(repo: Path, rel_dir: str, name: str) -> None:
    """Write a minimal spec-valid SKILL.md inside repo at rel_dir/<name>/."""
    p = repo / rel_dir / name / "SKILL.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(SKILL_FRONTMATTER.format(name=name))


def _last_commit(repo: Path, *fmt_args: str) -> str:
    r = subprocess.run(
        ["git", "log", "-1", *fmt_args],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    )
    return r.stdout


def _make_source(
    sync_mod,
    name: str,
    upstream: Path,
    src_path: str,
    dest_path: str,
    squash: bool = False,
):
    # Default squash=False in tests so the pre-existing fine-grained
    # assertions (per-commit author preservation, subject prefix on each
    # import) keep working. Squash-mode tests opt in explicitly.
    return sync_mod.Source.from_dict(
        {
            "name": name,
            "url": upstream.resolve().as_uri(),
            "branch": "main",
            "src_path": src_path,
            "dest_path": dest_path,
            "squash": squash,
        }
    )


# ---------- commits_touching_path ----------


def test_commits_touching_path_filters_unrelated(
    sync_mod, make_upstream, commit_file, file_url, tmp_path
):
    """A commit that only touches files OUTSIDE src_path must be omitted
    from the import range — that's the whole premise of idempotent state."""
    up = make_upstream("up")
    _skill(up, "skills", "alpha")
    subprocess.run(
        ["git", "add", "-A"], cwd=up, check=True, capture_output=True
    )
    sha_init = commit_file(up, "skills/alpha/notes.md", "n1", "init alpha")
    sha_outside = commit_file(up, "other/file.md", "x", "outside src_path")
    sha_inside = commit_file(up, "skills/alpha/notes.md", "n2", "update alpha")

    # Fetch into the cache and run the filter.
    src = _make_source(sync_mod, "src-a", up, "skills", "imported")
    cache, head = sync_mod.fetch_upstream(src)
    shas = sync_mod.commits_touching_path(cache, src.src_path, None, head)
    # Only the two skills/ commits — the outside commit is excluded.
    assert sha_outside not in shas
    assert sha_init in shas
    assert sha_inside in shas
    # Reverse order: oldest first.
    assert shas.index(sha_init) < shas.index(sha_inside)


# ---------- sync_one end-to-end ----------


def test_sync_one_first_run_imports_history(
    sync_mod, make_upstream, commit_file
):
    """First-ever sync against a fresh dest repo imports every upstream
    commit that touched src_path."""
    up = make_upstream("up")
    sha1 = commit_file(
        up,
        "skills/alpha/SKILL.md",
        SKILL_FRONTMATTER.format(name="alpha"),
        "add alpha skill",
        author_name="Alice",
        author_email="alice@example.invalid",
    )
    sha2 = commit_file(
        up,
        "skills/beta/SKILL.md",
        SKILL_FRONTMATTER.format(name="beta"),
        "add beta skill",
        author_name="Bob",
        author_email="bob@example.invalid",
    )

    src = _make_source(sync_mod, "src-a", up, "skills", "imported")
    state = {"version": 1, "sources": {}}
    result = sync_mod.sync_one(src, state, sync_mod.dest_repo, dry_run=False)

    assert result.status == "synced", result.message
    assert result.commits_imported == 2
    # dest tree contains both skills, copied under dest_path.
    assert (sync_mod.dest_repo / "imported" / "alpha" / "SKILL.md").exists()
    assert (sync_mod.dest_repo / "imported" / "beta" / "SKILL.md").exists()
    # State advanced to the newest touching commit (sha2).
    assert state["sources"][src.state_key]["last_sha"] == sha2


def test_sync_one_rerun_no_changes_is_up_to_date(
    sync_mod, make_upstream, commit_file
):
    up = make_upstream("up")
    commit_file(
        up,
        "skills/alpha/SKILL.md",
        SKILL_FRONTMATTER.format(name="alpha"),
        "add alpha",
    )

    src = _make_source(sync_mod, "src-a", up, "skills", "imported")
    state = {"version": 1, "sources": {}}

    sync_mod.sync_one(src, state, sync_mod.dest_repo, dry_run=False)
    pre_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    # Run again with no upstream changes.
    result2 = sync_mod.sync_one(src, state, sync_mod.dest_repo, dry_run=False)
    post_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    assert result2.status == "up-to-date"
    assert result2.commits_imported == 0
    assert pre_head == post_head, "HEAD must not advance on up-to-date rerun"


def test_sync_one_upstream_changes_outside_src_path_are_idempotent(
    sync_mod, make_upstream, commit_file
):
    """
    Regression guard for PR #12 / the whole motivation of idempotent state:

    If the upstream advances with commits that do NOT touch src_path, the
    next sync run must be a no-op. state.json.last_sha stays pinned to the
    newest commit that DID touch src_path — not to the upstream tip.
    Without this, every unrelated upstream commit would produce an empty
    state-bump PR.
    """
    up = make_upstream("up")
    sha_touch = commit_file(
        up,
        "skills/alpha/SKILL.md",
        SKILL_FRONTMATTER.format(name="alpha"),
        "add alpha",
    )

    src = _make_source(sync_mod, "src-a", up, "skills", "imported")
    state = {"version": 1, "sources": {}}
    sync_mod.sync_one(src, state, sync_mod.dest_repo, dry_run=False)
    assert state["sources"][src.state_key]["last_sha"] == sha_touch

    # Upstream advances with an UNRELATED commit.
    commit_file(up, "other/README.md", "hello", "change outside src_path")

    pre_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    result = sync_mod.sync_one(src, state, sync_mod.dest_repo, dry_run=False)
    post_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    assert result.status == "up-to-date"
    assert pre_head == post_head
    # Critically: last_sha MUST still point at sha_touch, not the new tip.
    assert state["sources"][src.state_key]["last_sha"] == sha_touch


def test_sync_one_incremental_import_of_new_touching_commit(
    sync_mod, make_upstream, commit_file
):
    up = make_upstream("up")
    commit_file(
        up,
        "skills/alpha/SKILL.md",
        SKILL_FRONTMATTER.format(name="alpha"),
        "add alpha",
    )

    src = _make_source(sync_mod, "src-a", up, "skills", "imported")
    state = {"version": 1, "sources": {}}
    sync_mod.sync_one(src, state, sync_mod.dest_repo, dry_run=False)

    # Second-run commit that DOES touch src_path.
    sha2 = commit_file(
        up,
        "skills/alpha/SKILL.md",
        SKILL_FRONTMATTER.format(name="alpha") + "\nMore body.\n",
        "tweak alpha",
    )

    result = sync_mod.sync_one(src, state, sync_mod.dest_repo, dry_run=False)
    assert result.status == "synced", result.message
    assert result.commits_imported == 1
    assert state["sources"][src.state_key]["last_sha"] == sha2


def test_sync_one_preserves_author_and_applies_subject_prefix(
    sync_mod, make_upstream, commit_file
):
    up = make_upstream("up")
    commit_file(
        up,
        "skills/alpha/SKILL.md",
        SKILL_FRONTMATTER.format(name="alpha"),
        "add alpha skill",
        author_name="Original Author",
        author_email="orig@example.invalid",
    )

    src = _make_source(sync_mod, "src-a", up, "skills", "imported")
    state = {"version": 1, "sources": {}}
    sync_mod.sync_one(src, state, sync_mod.dest_repo, dry_run=False)

    # Inspect the last commit on dest. The state-bump commit is on top
    # of the import, so we need the commit just before HEAD.
    log_out = subprocess.run(
        [
            "git",
            "log",
            "--format=%an%x00%ae%x00%s%x00%b%x01",
            "-3",
        ],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    entries = [e for e in log_out.split("\x01") if e.strip()]
    # Multiple commits start with `[src-a] ` — the state-bump commit
    # (authored by the bot) and the actual import. We want the import,
    # which is the one that is NOT a `chore(sync):` state-bump.
    import_entry = next(
        e for e in entries
        if "\x00[src-a] " in e and "chore(sync)" not in e
    )
    author_name, author_email, subject, body = (
        x.strip("\n") for x in import_entry.split("\x00", 3)
    )

    assert author_name == "Original Author"
    assert author_email == "orig@example.invalid"
    assert subject == "[src-a] add alpha skill"
    # Provenance trailers appended.
    assert "Source-Repo:" in body
    assert "Source-Commit:" in body
    # Committer (i.e. the bot) sign-off.
    assert "Signed-off-by: test-bot <test-bot@example.invalid>" in body


def test_sync_one_rerun_does_not_double_prefix(
    sync_mod, make_upstream, commit_file
):
    """Guard: regardless of run count, no imported commit's subject
    ever starts with `[src-a] [src-a] ...`. Combined with the
    idempotence test, this locks in both halves of prefix safety
    (don't re-import AND don't re-prefix if anything ever does)."""
    up = make_upstream("up")
    commit_file(
        up,
        "skills/alpha/SKILL.md",
        SKILL_FRONTMATTER.format(name="alpha"),
        "add alpha",
    )
    src = _make_source(sync_mod, "src-a", up, "skills", "imported")
    state = {"version": 1, "sources": {}}

    sync_mod.sync_one(src, state, sync_mod.dest_repo, dry_run=False)
    sync_mod.sync_one(src, state, sync_mod.dest_repo, dry_run=False)

    subjects = subprocess.run(
        ["git", "log", "--format=%s"],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.splitlines()
    # No subject should start with `[src-a] [src-a] `.
    assert not any(s.startswith("[src-a] [src-a] ") for s in subjects), subjects


def test_sync_one_dry_run_does_not_mutate(
    sync_mod, make_upstream, commit_file
):
    up = make_upstream("up")
    commit_file(
        up,
        "skills/alpha/SKILL.md",
        SKILL_FRONTMATTER.format(name="alpha"),
        "add alpha",
    )
    src = _make_source(sync_mod, "src-a", up, "skills", "imported")
    state = {"version": 1, "sources": {}}

    pre_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    result = sync_mod.sync_one(src, state, sync_mod.dest_repo, dry_run=True)
    post_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    assert result.status == "synced"
    assert result.commits_imported == 1
    assert pre_head == post_head, "dry-run must not alter dest repo"
    # state.json must not have been bumped either.
    assert src.state_key not in state["sources"]


def test_sync_one_validation_failure_resets_dest_and_keeps_state(
    sync_mod, make_upstream, commit_file
):
    """
    Import a commit whose SKILL.md is spec-invalid. sync_one must:
      - hard-reset dest to the pre-sync HEAD,
      - leave state.json unchanged (so next run retries, doesn't skip),
      - return a failure result.
    """
    up = make_upstream("up")
    # Frontmatter missing `description` — the validator will reject it.
    commit_file(
        up,
        "skills/bad/SKILL.md",
        "---\nname: bad\n---\n# body\n",
        "add bad skill",
    )

    src = _make_source(sync_mod, "src-a", up, "skills", "imported")
    state = {"version": 1, "sources": {}}

    pre_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    result = sync_mod.sync_one(src, state, sync_mod.dest_repo, dry_run=False)
    post_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    assert result.status == "failed"
    assert pre_head == post_head, (
        "dest HEAD must be reset to pre-sync on failure"
    )
    # state must not be bumped for this source.
    assert src.state_key not in state["sources"]
    # Dest tree must be clean (the partial import file is gone).
    assert not (sync_mod.dest_repo / "imported" / "bad" / "SKILL.md").exists()


# ---------- squash mode ----------


def test_sync_one_squash_collapses_multiple_commits_into_one(
    sync_mod, make_upstream, commit_file
):
    """
    With squash=True, a single sync run that imports N upstream commits
    must produce exactly ONE import commit on dest (plus the state-bump
    chore commit on top). The squashed commit:
      - has `[src-a] sync: import N upstream commits` subject,
      - lists every upstream subject in its body,
      - carries a Co-authored-by trailer for every distinct original
        author,
      - pins Source-Commit to the newest upstream sha.
    """
    up = make_upstream("up")
    commit_file(
        up,
        "skills/alpha/SKILL.md",
        SKILL_FRONTMATTER.format(name="alpha"),
        "add alpha skill",
        author_name="Alice",
        author_email="alice@example.invalid",
    )
    commit_file(
        up,
        "skills/alpha/SKILL.md",
        SKILL_FRONTMATTER.format(name="alpha") + "\nExtra body\n",
        "tweak alpha",
        author_name="Bob",
        author_email="bob@example.invalid",
    )
    sha3 = commit_file(
        up,
        "skills/alpha/SKILL.md",
        SKILL_FRONTMATTER.format(name="alpha") + "\nMore extra body\n",
        "polish alpha",
        author_name="Alice",  # duplicate author — must only appear once
        author_email="alice@example.invalid",
    )

    src = _make_source(
        sync_mod, "src-a", up, "skills", "imported", squash=True
    )
    state = {"version": 1, "sources": {}}
    pre_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    result = sync_mod.sync_one(src, state, sync_mod.dest_repo, dry_run=False)
    assert result.status == "synced", result.message
    assert result.commits_imported == 3

    # Between pre_head and HEAD there must be EXACTLY two commits:
    # the squashed import + the state-bump chore.
    commits = subprocess.run(
        ["git", "rev-list", "--reverse", f"{pre_head}..HEAD"],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.split()
    assert len(commits) == 2, (
        f"expected 2 commits (squash + state-bump), got {len(commits)}"
    )

    # Inspect the squashed import commit (not the chore on top).
    imported_sha = commits[0]
    show = subprocess.run(
        ["git", "show", "--no-patch", "--format=%s%x00%b", imported_sha],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    subject, body = show.split("\x00", 1)
    subject = subject.rstrip("\n")

    assert subject.startswith("[src-a] sync: import 3 upstream commits ")
    assert "add alpha skill" in body
    assert "tweak alpha" in body
    assert "polish alpha" in body
    # Provenance pins to newest upstream.
    assert f"Source-Commit: {sha3}" in body
    assert "Source-Repo:" in body
    # Both distinct authors are co-authors; Alice only once despite two commits.
    assert "Co-authored-by: Alice <alice@example.invalid>" in body
    assert "Co-authored-by: Bob <bob@example.invalid>" in body
    assert body.count("Co-authored-by: Alice") == 1
    # Bot sign-off present.
    assert "Signed-off-by: test-bot <test-bot@example.invalid>" in body


def test_sync_one_squash_rerun_with_no_changes_is_up_to_date(
    sync_mod, make_upstream, commit_file
):
    """Squash mode must still be idempotent — a rerun with no upstream
    changes is a no-op, HEAD does not advance."""
    up = make_upstream("up")
    commit_file(
        up,
        "skills/alpha/SKILL.md",
        SKILL_FRONTMATTER.format(name="alpha"),
        "add alpha",
    )
    src = _make_source(
        sync_mod, "src-a", up, "skills", "imported", squash=True
    )
    state = {"version": 1, "sources": {}}

    sync_mod.sync_one(src, state, sync_mod.dest_repo, dry_run=False)
    head1 = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    result2 = sync_mod.sync_one(src, state, sync_mod.dest_repo, dry_run=False)
    head2 = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    assert result2.status == "up-to-date"
    assert head1 == head2, "rerun with no changes must not advance HEAD"


def test_sync_one_squash_is_per_source_not_cross_source(
    sync_mod, make_upstream, commit_file
):
    """
    Squash is scoped to ONE source's imports. Running two sources in
    sequence must produce one squashed commit per source (plus their
    state-bumps) — the second source's squash must NOT eat the first
    source's commits.
    """
    up_a = make_upstream("up-a")
    commit_file(
        up_a,
        "skills/alpha/SKILL.md",
        SKILL_FRONTMATTER.format(name="alpha"),
        "add alpha v1",
    )
    commit_file(
        up_a,
        "skills/alpha/SKILL.md",
        SKILL_FRONTMATTER.format(name="alpha") + "\nbody\n",
        "add alpha v2",
    )

    up_b = make_upstream("up-b")
    commit_file(
        up_b,
        "skills/beta/SKILL.md",
        SKILL_FRONTMATTER.format(name="beta"),
        "add beta v1",
    )
    commit_file(
        up_b,
        "skills/beta/SKILL.md",
        SKILL_FRONTMATTER.format(name="beta") + "\nbody\n",
        "add beta v2",
    )

    src_a = _make_source(
        sync_mod, "src-a", up_a, "skills", "imported-a", squash=True
    )
    src_b = _make_source(
        sync_mod, "src-b", up_b, "skills", "imported-b", squash=True
    )
    state = {"version": 1, "sources": {}}

    pre_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    r1 = sync_mod.sync_one(src_a, state, sync_mod.dest_repo, dry_run=False)
    r2 = sync_mod.sync_one(src_b, state, sync_mod.dest_repo, dry_run=False)
    assert r1.status == "synced" and r1.commits_imported == 2
    assert r2.status == "synced" and r2.commits_imported == 2

    # Collect all commit subjects between pre_head..HEAD.
    subjects = subprocess.run(
        ["git", "log", "--format=%s", f"{pre_head}..HEAD", "--reverse"],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.splitlines()

    # Expected 4 commits total: squash-a, chore-a, squash-b, chore-b.
    assert len(subjects) == 4, subjects
    assert subjects[0].startswith("[src-a] sync: import 2 upstream commits")
    assert subjects[1].startswith("[src-a] chore(sync):")
    assert subjects[2].startswith("[src-b] sync: import 2 upstream commits")
    assert subjects[3].startswith("[src-b] chore(sync):")
    # Both skill trees present on disk.
    assert (sync_mod.dest_repo / "imported-a" / "alpha" / "SKILL.md").exists()
    assert (sync_mod.dest_repo / "imported-b" / "beta" / "SKILL.md").exists()


# ---------- mode=pr ----------
#
# pr-mode is used when the sync engine proposes imports into another repo
# as a PR rather than mirroring into the hub. Key behavioural differences
# from push-mode being locked in below:
#
#   * imports land on a per-source branch `skills-sync/<name>` forked from
#     the pre-sync HEAD; original branch is restored on return,
#   * state.json is NOT modified and NO `chore(sync): advance state` commit
#     is created (the hub owns sync state, not the PR target),
#   * SourceResult.dest_branch is populated with the per-source branch name
#     (workflow reads this to force-push + open/update a PR),
#   * on failure, dest_branch is cleared and the per-source branch is reset
#     to the pre-sync base so no empty/half-applied PR is ever proposed.


def _current_branch(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def _branch_exists(repo: Path, name: str) -> bool:
    r = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
        cwd=repo,
    )
    return r.returncode == 0


def test_sync_one_pr_mode_creates_per_source_branch_and_leaves_state_untouched(
    sync_mod, make_upstream, commit_file
):
    """pr-mode: imports land on skills-sync/<name>, state.json untouched,
    no chore(sync) state-bump commit, original branch restored on return,
    and SourceResult.dest_branch is set to the per-source branch."""
    up = make_upstream("up")
    commit_file(
        up,
        "skills/alpha/SKILL.md",
        SKILL_FRONTMATTER.format(name="alpha"),
        "add alpha",
    )

    src = _make_source(sync_mod, "alpha-src", up, "skills", "imported")
    state = {"version": 1, "sources": {}}

    base_before = _current_branch(sync_mod.dest_repo)
    base_head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    result = sync_mod.sync_one(
        src, state, sync_mod.dest_repo, dry_run=False, mode=sync_mod.MODE_PR
    )

    assert result.status == "synced", result.message
    assert result.commits_imported == 1
    assert result.dest_branch == "skills-sync/alpha-src"

    # We're back on the original branch and its HEAD is unchanged: pr-mode
    # must not mutate the branch we were called on.
    assert _current_branch(sync_mod.dest_repo) == base_before
    base_head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    assert base_head_after == base_head_before

    # The per-source branch exists and IS ahead of base — this is what the
    # workflow would push + open a PR for.
    assert _branch_exists(sync_mod.dest_repo, "skills-sync/alpha-src")
    pr_head = subprocess.run(
        ["git", "rev-parse", "skills-sync/alpha-src"],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    assert pr_head != base_head_after, (
        "pr branch must carry the imported commit on top of base"
    )

    # state.json was NOT touched: pr-mode leaves canonical state alone
    # because the hub owns it, not this proposing run.
    assert state["sources"] == {}

    # And no chore(sync): advance state commit was made on the pr branch.
    subjects = subprocess.run(
        [
            "git",
            "log",
            "--format=%s",
            f"{base_head_after}..skills-sync/alpha-src",
        ],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.splitlines()
    assert subjects, "expected at least one commit on pr branch"
    assert not any(
        s.startswith("[alpha-src] chore(sync):") for s in subjects
    ), f"pr-mode must not emit state-bump commits, got: {subjects}"


def test_sync_one_pr_mode_two_sources_fork_from_same_base(
    sync_mod, make_upstream, commit_file
):
    """Two pr-mode sources in one run must each fork their own
    skills-sync/<name> branch off the SAME base (not off each other).
    Proves original_branch is restored between sources so cross-source
    contamination can't happen."""
    up_a = make_upstream("up-a")
    commit_file(
        up_a,
        "skills/alpha/SKILL.md",
        SKILL_FRONTMATTER.format(name="alpha"),
        "add alpha",
    )
    up_b = make_upstream("up-b")
    commit_file(
        up_b,
        "skills/beta/SKILL.md",
        SKILL_FRONTMATTER.format(name="beta"),
        "add beta",
    )

    src_a = _make_source(sync_mod, "a", up_a, "skills", "imported-a")
    src_b = _make_source(sync_mod, "b", up_b, "skills", "imported-b")
    state = {"version": 1, "sources": {}}

    base_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    r_a = sync_mod.sync_one(
        src_a, state, sync_mod.dest_repo, dry_run=False, mode=sync_mod.MODE_PR
    )
    r_b = sync_mod.sync_one(
        src_b, state, sync_mod.dest_repo, dry_run=False, mode=sync_mod.MODE_PR
    )

    assert r_a.dest_branch == "skills-sync/a"
    assert r_b.dest_branch == "skills-sync/b"

    # Each per-source branch forks from exactly `base_head` — i.e. the B
    # branch must NOT contain A's imports.
    a_parent = subprocess.run(
        ["git", "rev-parse", "skills-sync/a^"],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    b_parent = subprocess.run(
        ["git", "rev-parse", "skills-sync/b^"],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    assert a_parent == base_head, f"a forked from {a_parent}, not {base_head}"
    assert b_parent == base_head, f"b forked from {b_parent}, not {base_head}"

    # Trees are isolated: branch a has alpha only, branch b has beta only.
    a_files = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "skills-sync/a"],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    b_files = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "skills-sync/b"],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    assert "imported-a/alpha/SKILL.md" in a_files
    assert "imported-b/beta/SKILL.md" not in a_files
    assert "imported-b/beta/SKILL.md" in b_files
    assert "imported-a/alpha/SKILL.md" not in b_files


def test_sync_one_pr_mode_validation_failure_clears_dest_branch(
    sync_mod, make_upstream, commit_file
):
    """If the imported tree fails validation in pr-mode, the per-source
    branch is reset to base (no partial commits survive) and
    SourceResult.dest_branch is cleared so the workflow doesn't open an
    empty/broken PR."""
    up = make_upstream("up")
    # Upstream SKILL.md is missing a 'description' field -> fails spec
    # validation. This is the same trigger that test_sync_one_validation_*
    # uses in push-mode.
    bad = "---\nname: broken\n---\n# broken\n"
    commit_file(up, "skills/broken/SKILL.md", bad, "add broken skill")

    src = _make_source(sync_mod, "broken-src", up, "skills", "imported")
    state = {"version": 1, "sources": {}}

    base_before = _current_branch(sync_mod.dest_repo)
    base_head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    result = sync_mod.sync_one(
        src, state, sync_mod.dest_repo, dry_run=False, mode=sync_mod.MODE_PR
    )

    assert result.status == "failed", result.message
    # dest_branch cleared: no PR should be opened for a failed import.
    assert result.dest_branch == ""
    # state untouched.
    assert state["sources"] == {}
    # Original branch restored, base HEAD untouched.
    assert _current_branch(sync_mod.dest_repo) == base_before
    base_head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=sync_mod.dest_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    assert base_head_after == base_head_before
    # The pr branch, if it exists, points at base (no imports on it).
    if _branch_exists(sync_mod.dest_repo, "skills-sync/broken-src"):
        pr_head = subprocess.run(
            ["git", "rev-parse", "skills-sync/broken-src"],
            cwd=sync_mod.dest_repo,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        assert pr_head == base_head_before, (
            "failed pr-mode branch must be reset to base, got commits on it"
        )


# ---------- CLI entrypoint: --results-json + --mode pr ----------


def test_cli_main_results_json_pr_mode(
    sync_mod, make_upstream, commit_file, monkeypatch, tmp_path
):
    """End-to-end CLI invocation: `opensearch-skills-sync --mode pr
    --results-json PATH` against a sources dir with two sources. Verifies
    the JSON payload has the fields the workflow iterates (name, status,
    dest_branch, commits_imported) and that pr-mode isolation holds (no
    state.json write, per-source branches exist, original branch restored).
    """
    import json as _json

    # Two upstream repos, each contributing a skill.
    up_a = make_upstream("up_a")
    commit_file(
        up_a,
        "skills/alpha/SKILL.md",
        SKILL_FRONTMATTER.format(name="alpha"),
        "seed alpha",
    )
    up_b = make_upstream("up_b")
    commit_file(
        up_b,
        "skills/beta/SKILL.md",
        SKILL_FRONTMATTER.format(name="beta"),
        "seed beta",
    )

    # Two source YAML files in a sandboxed sources_dir.
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir()
    (sources_dir / "alpha-src.yaml").write_text(
        f"name: alpha-src\n"
        f"url: {up_a.resolve().as_uri()}\n"
        "branch: main\n"
        "src_path: skills\n"
        "dest_path: imported-a\n"
        "squash: false\n"
    )
    (sources_dir / "beta-src.yaml").write_text(
        f"name: beta-src\n"
        f"url: {up_b.resolve().as_uri()}\n"
        "branch: main\n"
        "src_path: skills\n"
        "dest_path: imported-b\n"
        "squash: false\n"
    )

    results_json = tmp_path / "results.json"
    # Invoke main() by patching sys.argv. main() uses argparse so this is
    # the most faithful reproduction of the workflow's CLI invocation.
    monkeypatch.setattr(
        "sys.argv",
        [
            "opensearch-skills-sync",
            "--sources-dir",
            str(sources_dir),
            "--mode",
            "pr",
            "--results-json",
            str(results_json),
        ],
    )

    # dest_repo fixture made this repo the cwd-independent REPO_ROOT;
    # main() also shells `git` from REPO_ROOT so no chdir needed.
    base_before = _current_branch(sync_mod.dest_repo)
    exit_code = sync_mod.main()
    assert exit_code == 0

    # Results JSON: one entry per source, required keys present.
    assert results_json.exists()
    payload = _json.loads(results_json.read_text())
    assert isinstance(payload, list) and len(payload) == 2
    by_name = {r["name"]: r for r in payload}
    for expected in ("alpha-src", "beta-src"):
        assert expected in by_name, f"missing {expected}"
        r = by_name[expected]
        # Keys the workflow iterates on.
        for key in (
            "name",
            "status",
            "commits_imported",
            "new_head",
            "dest_branch",
            "message",
            "src_path",
            "dest_path",
            "url",
        ):
            assert key in r, f"{expected}: missing {key}"
        assert r["status"] == "synced", r
        assert r["commits_imported"] >= 1
        assert r["dest_branch"] == f"skills-sync/{expected}"

    # pr-mode invariants: state.json untouched, original branch restored,
    # per-source branches exist with commits on them.
    assert not (sync_mod.dest_repo / "sync" / "state.json").exists(), (
        "pr-mode must not write state.json"
    )
    assert _current_branch(sync_mod.dest_repo) == base_before
    for name in ("alpha-src", "beta-src"):
        assert _branch_exists(sync_mod.dest_repo, f"skills-sync/{name}")
