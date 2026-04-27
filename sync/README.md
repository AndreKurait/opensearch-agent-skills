# Skill Sync

This directory contains the machinery that mirrors skill subdirectories
from upstream repos into this one.

## Files

- `sources/*.yaml` — one file per upstream source (repo URL, branch,
  source path, destination path). One-file-per-source keeps merge
  conflicts localized when multiple sources are added in parallel.
- `state.json` — last-synced upstream commit SHA per source. Committed
  to the repo so subsequent runs are incremental. Do not hand-edit
  unless you also want to reset the import window.

The sync **engine** lives in [`../sync-bot/`](../sync-bot/) as a
dedicated uv-managed Python package (`opensearch-skills-sync`) with
its own `pyproject.toml` / `uv.lock`. Invoke it via `uv run
--project sync-bot opensearch-skills-sync …` — see
`sync-bot/README.md` for details.

## What the sync does

On each run, for every source:

1. Clone-or-fetch the upstream into `.sync-cache/<name>` (blob-filtered,
   so it stays small even for large repos).
2. Read `state.json` for the last-synced upstream SHA.
3. Enumerate upstream commits that touched `src_path` since then:
   `git log --reverse --no-merges <last>..<head> -- <src_path>`.
4. For each such commit:
   - `git format-patch -1 --relative=<src_path>` produces a patch whose
     paths are relative to the source subdirectory.
   - `git am --directory=<dest_path>` applies that patch under the
     destination path in this repo. `git am` preserves the **original
     author** and sets the **committer** to the sync bot.
   - `git commit --amend` rewrites the subject to
     `[<source-name>] <original subject>` (LLVM-monorepo style) and
     appends provenance trailers:
     ```
     Source-Repo:   <upstream url>
     Source-Commit: <upstream sha>
     Co-authored-by: <original author>
     Signed-off-by: <sync bot>
     ```
     Re-runs are idempotent — an already-prefixed subject is not
     re-prefixed.
5. Validate the resulting dest tree against the Agent Skills Spec. A
   spec violation rolls back this source's imports and opens a tracking
   issue.
6. Update `state.json` and commit the advance as
   `[<source-name>] chore(sync): advance state to <sha>`. Push is
   handled by the workflow.

Original commit messages (minus the one-line subject prefix), dates,
and authorship are preserved. The GitHub contributor graph for this
repo credits upstream authors.

## Adding a new source

The whole flow is: **drop one YAML file into `sources/`, push a PR,
done.** No manual workflow dispatches, no local sync run required.

1. Create `sync/sources/<short-name>.yaml` (~20 lines; copy any
   existing file as a template):

   ```yaml
   # sync/sources/some-project.yaml
   name: some-project                              # stable id; used in state key and commit prefix
   url: https://github.com/org/some-project.git
   branch: main
   src_path: skills                                # subdir in upstream to mirror
   dest_path: skills/some-project                  # where it lands here (sibling of authored skills)
   ```

2. Commit and push on a branch, open a PR.

3. `Sync Skills` auto-fires on the PR (it watches `sync/sources/**`).
   It clones the upstream, replays the history that touched `src_path`
   into `dest_path`, pushes those commits onto your PR branch, and
   re-dispatches CI + dry-run validation against the new HEAD.

4. Review the bot's added commits, squash or leave as-is when merging.

Files are loaded in lexicographic order, so the filename doubles as the
sync-order knob if you ever need one. Duplicate `name:` across files is
a hard error.

PRs opened from forks skip the auto-push (GitHub won't let workflows
write to forks) but still run dry-run validation, so config mistakes
surface before merge.

### Previewing the replayed commits on a fork PR

Fork PRs don't get the auto-push because GitHub only issues read-only
`GITHUB_TOKEN`s for `pull_request` events from forks — the "Allow
edits and access to secrets by maintainers" checkbox on the PR does
**not** change that (it applies to human pushes only). The platform
enforces this at the token level to prevent a malicious PR from
rewriting the workflow and exfiltrating secrets.

Escape hatch: the fork owner can dispatch `Sync Skills` on their own
fork against the PR branch. That run executes under the fork's context
where `GITHUB_TOKEN` has write access, and any replayed commits land
on the PR branch (which auto-updates the upstream PR since the branch
is the PR head).

```bash
# Dispatch on your fork, scoped to the new source, against the PR branch.
gh workflow run sync-skills.yml \
  --repo <you>/opensearch-agent-skills \
  --ref <your-pr-branch> \
  -f only=<new-source-name>
```

Reviewers then see the real imported tree on the PR before merging.
This is a fork-owner-only action — nothing an upstream reviewer can
trigger from their side.

## Running locally

```bash
# Sync all sources
uv run --project sync-bot opensearch-skills-sync

# Sync just one source
uv run --project sync-bot opensearch-skills-sync --only some-project

# Dry-run (list what would be imported, don't commit)
uv run --project sync-bot opensearch-skills-sync --dry-run

# Custom sources directory (useful for tests)
uv run --project sync-bot opensearch-skills-sync --sources-dir path/to/sources
```

## Failure handling

Each source is processed independently. A failure (bad patch, upstream
fetch error, conflict, spec-validation violation) aborts any in-progress
`git am`, hard-resets that source's imports, and moves on. Successful
sources still commit. The script exits nonzero if any source failed, so
CI marks the run failed — but the good commits are already in HEAD and
will be pushed.

Failed sources open (or update) a GitHub issue labelled
`sync-failure` + `sync-source:<name>`. The issue stays open while the
source keeps failing with the same error (no comment spam); a new error
posts a comment; a successful sync auto-closes the issue.

## Resetting a source

To force a full re-import of an upstream, delete its entry from
`state.json.sources` and commit. The next run will treat it as a
first-time sync and replay the upstream history (bounded by the
blob-filtered clone).

## pr-mode (per-source PRs)

The default sync flow above is **push mode** — the bot lands commits
directly on the target branch, one shared state file, one combined
history. That's the right choice for the hub repo itself (mirror
behaviour, immediate availability to agents).

The workflow also supports **pr mode** (`workflow_dispatch` → `mode:
pr`), which is what the sync-bot framework ships as its externally-
facing product: for every source with new upstream commits, the bot
force-pushes a `skills-sync/<source>` branch and opens (or updates) a
PR from it into the target branch. Each source gets its own PR so
reviewers see a focused diff and one failing source doesn't block the
others.

Invariants:

- **No `state.json` writes.** pr-mode derives the last-synced SHA from
  the `Source-Commit:` trailer on the tip of `skills-sync/<source>`,
  falling back to the same trailer on the target branch, falling back
  to "first run". The state is the git history itself, not a file.
- **Branch reset on every run.** `skills-sync/<source>` is reset to
  the target branch at the start of each run and then has the
  upstream commits replayed on top. Manual commits on it are
  discarded — see `SYNCED_SKILL_POLICY.md` for the "edit upstream
  instead" flow.
- **One PR per source.** Even if N upstreams moved in one run, the
  workflow opens/updates N PRs. A failed source only fails its own
  PR; the others still publish.

To flip the hub itself to pr-mode, just invoke the workflow with
`mode: pr`. To invoke it from an automation, `gh workflow run
sync-skills.yml -f mode=pr`.

### Repo-level prereq for pr-mode

pr-mode uses `GITHUB_TOKEN` to open PRs, which requires the repo to
allow Actions to create pull requests. Enable it once at **Settings →
Actions → General → Workflow permissions → "Allow GitHub Actions to
create and approve pull requests"**. A user/org-level toggle with the
same name does **not** override a repo-level OFF — GitHub evaluates
the most restrictive. Symptom if missing: `gh pr create` fails with
`GitHub Actions is not permitted to create or approve pull requests`.

## Editing a synced skill

**Don't edit synced files in this repo** — fix the upstream instead,
and the next sync tick will carry the fix here. See
[`SYNCED_SKILL_POLICY.md`](./SYNCED_SKILL_POLICY.md) for the full
rationale and the "break the sync" escape hatch for legitimate
divergence cases.
