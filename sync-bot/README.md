# sync-bot

Installable Python package that implements the skill-sync workflow for
`opensearch-agent-skills`. For *what* the sync does and the *data* it
operates on, see [`../sync/README.md`](../sync/README.md). This directory
contains only the executable code and its tests.

## Layout

```
sync-bot/
├── pyproject.toml        ← declares runtime deps (pyyaml, skills-ref)
├── uv.lock               ← pinned resolution
├── src/
│   └── skills_sync/
│       ├── __init__.py   ← re-exports + console-script entry
│       ├── __main__.py   ← enables `python -m skills_sync`
│       └── main.py       ← all sync logic
└── tests/
    ├── conftest.py
    ├── test_sync_unit.py
    └── test_sync_integration.py
```

The data it manages stays at the repo root:

- `../sync/state.json` — committed high-water-mark of synced SHAs
- `../sync/sources/*.yaml` — source manifests (one file per upstream)

## Running

From the repository root:

```bash
# One-off dry run
uv run --project sync-bot opensearch-skills-sync --dry-run

# Full sync (writes state.json + creates commits)
uv run --project sync-bot opensearch-skills-sync

# Single source
uv run --project sync-bot opensearch-skills-sync --only anthropic-skill-creator

# Tests
uv run --project sync-bot pytest
```

Equivalent invocations:

```bash
uv run --project sync-bot python -m skills_sync --dry-run
```

## Repo-root resolution

The sync code lives in this package; the data (`sync/state.json`,
`sync/sources/`) lives at the repository root. The tool resolves the
root in this order:

1. `SYNC_REPO_ROOT` env var (absolute path) — used by tests and for
   out-of-tree invocations.
2. `git rev-parse --show-toplevel` from the current working directory.
3. The current working directory.

In CI and in normal `uv run --project sync-bot …` from the repo root,
path 2 always matches.

## Updating dependencies

```bash
# Add / bump a runtime dep
uv add --project sync-bot pyyaml@^7.0

# Regenerate the lockfile after editing pyproject.toml by hand
uv lock --project sync-bot
```
