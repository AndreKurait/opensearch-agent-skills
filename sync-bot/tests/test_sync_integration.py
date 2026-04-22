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
