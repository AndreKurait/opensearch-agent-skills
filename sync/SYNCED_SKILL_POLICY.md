# Synced Skill Policy

If you landed here from a link in a pull-request body, this document
explains **why editing a synced skill directly in this repo is almost
never what you want** — and what to do instead.

## TL;DR

Skills under a `dest_path` declared in `sync/sources/*.yaml` are
**mirrored from an upstream repo**. The sync bot reruns on a cron and
**replays upstream history on top of the destination branch**, which
overwrites any local edits on the next run.

- **Bug / feature / wording change** → fix it **upstream**, in the
  repo listed under `Source-Repo:` in the commit trailer. The fix will
  land here automatically on the next sync tick.
- **Policy exception** (must diverge from upstream) → see
  [Breaking the sync](#breaking-the-sync) below.
- **Deleting a skill** → delete the `sync/sources/<name>.yaml` entry
  and remove `dest_path` in the same PR; the bot will stop mirroring.

## Why not just edit here?

The sync bot is append-only on the upstream → hub edge: it takes every
upstream commit that touched `src_path` and replays it at `dest_path`.
It does **not** detect, preserve, or merge hub-local edits inside
`dest_path`. Two modes, same outcome for you:

- **push mode** (default for most sources): the bot commits directly
  onto `main`. A hub-local edit to a synced file will survive until
  the next upstream commit on the same file, at which point
  `git am` will hit a conflict and roll back that source's entire
  incremental window. Your edit wins the short term but **blocks all
  future upstream syncs** for that source until someone resolves it.
- **pr mode**: the bot resets `skills-sync/<source>` to `main` on every
  run and replays upstream on top. Any edit you push to that branch is
  discarded on the next cron tick. Edits to `main` under a pr-mode
  `dest_path` survive locally but diverge silently from upstream — the
  sync bot will keep opening PRs that revert your change.

Neither is good. Both produce the same message in code review: "this
file is synced; fix it upstream."

## How to tell if a skill is synced

Check `sync/sources/*.yaml`. If your file path starts with any
`dest_path` listed there, it's synced. Every commit the sync bot lands
also carries trailers:

```
Source-Repo:   https://github.com/<org>/<repo>.git
Source-Commit: <upstream sha>
Co-authored-by: <original author> <...>
```

`git log --format=%B <path> | grep '^Source-'` tells you quickly.

## Where to send the PR

1. Open the upstream repo from `Source-Repo:`.
2. Find the file at the path under the upstream's `src_path` that
   mirrors to this `dest_path`. (Example: this repo's
   `skills/opensearch-migrations/<x>` is the upstream's
   `skills/<x>`.)
3. PR the fix there. Once it merges, the next sync run (≤5 min for
   cron, immediate for `workflow_dispatch`) will carry it over.

## Breaking the sync

If you genuinely need to diverge from upstream — upstream is
unmaintained, the fix is blocked there, or the skill here needs a
hub-specific tweak — the supported path is to **detach the skill from
the sync**:

1. In the same PR: remove or rename the affected `dest_path` entry
   in `sync/sources/<source>.yaml` (or narrow `src_path` to exclude
   this skill).
2. Move the skill files out of the synced tree (e.g., rename the
   directory). This turns them into hub-authored skills.
3. Now edit freely.

Detaching is cheap and reversible — you can re-attach later by
restoring the mapping, at which point the next sync replays upstream
on top of the hub copy. It is **strictly better** than fighting the
bot in-place.

## Why this policy exists

Skills here get pulled by agents across many tasks. Silent divergence
between the hub copy and the upstream copy means an agent reads
different instructions in different contexts, which is worse than
either version alone. The sync bot's job is to keep that divergence
visible and auditable.

See `sync/README.md` for the mechanical details of how the sync works.
