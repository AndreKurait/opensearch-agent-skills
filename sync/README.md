# Skill Sync

This directory contains the machinery that mirrors skill subdirectories
from upstream repos into this one.

## Files

- `sync.yaml` — list of upstream sources (repo URL, branch, source path,
  destination path). Add one entry per upstream.
- `sync.py` — the sync engine. Runs on a schedule via GitHub Actions; can
  also be invoked locally.
- `state.json` — last-synced upstream commit SHA per source. Committed to
  the repo so subsequent runs are incremental. Do not hand-edit unless
  you also want to reset the import window.

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
   - `git commit --amend` appends provenance trailers:
     ```
     Source-Repo:   <upstream url>
     Source-Commit: <upstream sha>
     Co-authored-by: <original author>
     Signed-off-by: <sync bot>
     ```
5. Update `state.json` and commit the advance. Push is handled by the
   workflow.

Original commit messages, subjects, dates, and authorship are preserved.
The GitHub contributor graph for this repo will credit upstream authors.

## Adding a new source

Edit `sync.yaml`:

```yaml
sources:
  - name: some-project                               # stable identifier; used in state key
    url: https://github.com/org/some-project.git
    branch: main
    src_path: skills                                 # subdir in upstream to mirror
    dest_path: skills/external/some-project          # where it lands here
```

Then either wait for the cron tick, or trigger the workflow manually
(Actions → "Sync Skills" → Run workflow).

## Running locally

```bash
# Sync all sources
uv run --script sync/sync.py

# Sync just one source
uv run --script sync/sync.py --only some-project

# Dry-run (list what would be imported, don't commit)
uv run --script sync/sync.py --dry-run
```

## Failure handling

Each source is processed independently. A failure (bad patch, upstream
fetch error, conflict) aborts any in-progress `git am`, hard-resets that
source's imports, and moves on. Successful sources still commit. The
script exits nonzero if any source failed, so CI marks the run failed —
but the good commits are already in HEAD and will be pushed.

## Resetting a source

To force a full re-import of an upstream, delete its entry from
`state.json.sources` and commit. The next run will treat it as a
first-time sync and replay the upstream history (bounded by the
blob-filtered clone).
