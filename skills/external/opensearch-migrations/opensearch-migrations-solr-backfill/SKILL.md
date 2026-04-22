---
name: opensearch-migrations-solr-backfill
description: Develop and debug the Solr-to-OpenSearch backfill pipeline. Covers the Solr reader chain (SolrCore -> SegmentReader -> Document), the index-name mapping, and the fixture cache pitfalls that silently break tests.
metadata:
  tags:
    - opensearch-migrations
    - solr
    - backfill
    - lucene
---

# Solr-to-OpenSearch backfill

The Solr backfill path reuses the RFS machinery but swaps the Lucene
reader chain for a Solr-aware one. Core modules:

- `RFS/src/main/java/.../lucene/solr/` — Solr-specific readers
- `solrMigrationDevSandbox/` — local dev loop with docker-compose and a
  seeded SolrCloud cluster

## Known pitfalls

1. **Fixture cache key desync** — `SnapshotFixtureCache` keys on
   `(snapshot_name, index_uuid)` not the display index name. If a test
   rebuilds the snapshot with the same display name but the underlying
   index UUID changes, the cache returns a stale reader and `listCollections`
   comes back empty. Clear `/tmp/snapshot-fixture-cache/` between runs.
2. **SolrJ → OpenSearch request shape** — the Solr `content-type` header
   is `application/javabin` for binary responses; OpenSearch's bulk API
   refuses anything other than `application/x-ndjson`. Translation happens
   in `SolrToOsBulkTranslator`.
3. **Trie-encoded numerics** — Solr/Lucene 4 numeric fields are encoded as
   trie terms, not stored values. The backfill reconstructs them by
   decoding trie terms at read time; do not rely on `doc_values` being
   present.

## Verification

```bash
./gradlew :RFS:test --tests '*Solr*' --no-daemon
```

Covers the reader, bulk translator, and fixture cache paths.
