# Sync sources

One YAML file per upstream repo whose skills we mirror into this one.

## Add a new source (PR-driven — fully automated)

1. Copy any existing `*.yaml` in this directory as a template.
2. Edit the five fields (`name`, `url`, `branch`, `src_path`,
   `dest_path`). Keep `name` matching the filename for sanity.
3. Commit on a branch, push, open a PR.

That's it. The `Sync Skills` workflow auto-runs on every PR that
touches `sync/sources/**`: it replays the upstream history that
touched `src_path` into `dest_path`, pushes those commits onto your
PR branch, and re-runs CI + dry-run validation against the post-sync
HEAD. Review the result, then merge.

## Minimal template (21 lines)

```yaml
# Sync source: <short-name>
#
# Mirrors <upstream thing> from <upstream repo>.
#
# Schema:
#   name:      unique short id (filename, state key, commit prefix)
#   url:       upstream git URL
#   branch:    upstream branch to track
#   src_path:  subdirectory in the upstream repo whose history we import
#   dest_path: where that subdirectory lands in this repo
name: <short-name>
url: https://github.com/<org>/<repo>.git
branch: main
src_path: skills/<upstream-skill-dir>
# dest_path's leaf MUST match the upstream SKILL.md `name:` field —
# the Agent Skills Spec validator enforces this.
dest_path: skills/external/<upstream-skill-dir>
```

## Rules

- `name` must be unique across all files in this directory (hard error otherwise).
- `dest_path`'s leaf directory name must equal the upstream SKILL.md's
  `name:` field — the spec validator in `sync.py` enforces this and
  rolls back the import on mismatch.
- Files are processed in lexicographic order.

## Troubleshooting

If the auto-sync fails on your PR, `Sync Skills` will open (or update)
a tracking issue labelled `sync-failure` + `sync-source:<name>` with
the error. Fix the YAML, push again, and the issue auto-closes on the
next successful sync.

For full details on the sync engine and commit-rewriting semantics see
[../README.md](../README.md).
