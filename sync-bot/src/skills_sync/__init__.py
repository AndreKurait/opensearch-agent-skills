"""
skills_sync — import skill subdirectories from upstream repos into this one.

The executable entry point is `opensearch-skills-sync` (installed as a
console_script by this package's pyproject.toml) or equivalently
`python -m skills_sync`.

The module-level constants (REPO_ROOT, STATE_FILE, CACHE_DIR, SYNC_BOT_NAME,
SYNC_BOT_EMAIL, SOURCE_LABEL_PREFIX, …) are re-exported here so tests can
patch them without caring about the submodule layout.
"""

from __future__ import annotations

# Re-exports so tests and external callers can do `from skills_sync import X`
# without caring about the submodule layout.
#
# IMPORTANT: do NOT re-export `main` (the function) here. Doing so binds
# the name `main` in the package namespace and shadows the `skills_sync.main`
# submodule, making `import skills_sync.main` resolve to the function
# instead of the module. The CLI entry point `_cli` imports `main`
# lazily from `.main` so it doesn't need it at package level.
from .main import (  # noqa: F401  (re-exports for tests & external use)
    CACHE_DIR,
    DEFAULT_SOURCES_DIR,
    REPO_ROOT,
    SOURCE_LABEL_PREFIX,
    STATE_FILE,
    SYNC_BOT_EMAIL,
    SYNC_BOT_NAME,
    Source,
    _error_hash,
    _source_name_from_issue,
    commits_touching_path,
    fetch_upstream,
    format_validation_failures,
    load_sources_from_dir,
    prefix_subject,
    sync_one,
    validate_skill_tree,
)


def _cli() -> None:
    """Console-script entry point (pyproject.toml -> [project.scripts])."""
    import sys

    from .main import main

    sys.exit(main())


if __name__ == "__main__":  # pragma: no cover — `python -m skills_sync`
    _cli()
