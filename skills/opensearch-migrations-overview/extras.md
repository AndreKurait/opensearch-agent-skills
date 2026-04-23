# Extras: build caching

The `.gradle/` directory caches per-module. For CI, the opensearch-ci
Jenkins agents cache across runs via `actions-cache-v4`; local builds
typically use `~/.gradle/caches/`. If a build behaves strangely after a
toolchain change, nuke both and rebuild from cold.


## Test append for failure-isolation verification
