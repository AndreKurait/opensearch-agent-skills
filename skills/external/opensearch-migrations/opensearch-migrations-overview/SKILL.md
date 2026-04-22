---
name: opensearch-migrations-overview
description: High-level orientation for the opensearch-migrations project — what it is, the component layout (RFS, Traffic Capture, CDK), and how the modules fit together. Use as a starting point when an agent first enters the repo.
tags: [opensearch-migrations, orientation, architecture]
---

# opensearch-migrations — quick orientation

`opensearch-migrations` is a toolkit for migrating from Elasticsearch (5.x
through 8.x) and legacy OpenSearch clusters to newer OpenSearch / Amazon
OpenSearch Service targets with minimal downtime.

## Top-level modules you'll touch most

- `RFS/` — "Reindex From Snapshot". The bulk document backfill engine.
  Reads Lucene snapshots directly out of S3 and writes documents into the
  target cluster via the bulk API. Versioned readers live under
  `RFS/src/main/java/.../lucene/`.
- `TrafficCapture/` — live-traffic capture + replay. Captures production
  requests via a proxy (`trafficCaptureProxyServer`) and replays them
  against the target (`trafficReplayer`) to catch behavioral drift.
- `DocumentsFromSnapshotMigration/` — the standalone RFS worker image
  (container entrypoint for the Argo orchestration).
- `MetadataMigration/` — schema/template/index-metadata transforms.
- `deployment/cdk/` — AWS CDK stacks that provision the migration
  assistant into an AWS account.
- `deployment/k8s/` — Helm + Argo Workflows for on-cluster orchestration.
- `buildImages/` — **custom Elasticsearch test-fixture images** pinned to
  the JDK that specific ES majors require (ES 5→JDK 8, ES 6/7→JDK 11).
  DO NOT bump JDKs here; see skill `java-version-bump` for the carve-out.

## Entry points for typical tasks

- "Run the unit tests": `./gradlew :RFS:test :DocumentsFromSnapshotMigration:test`
- "Spin up a local demo": `TrafficCapture/dockerSolution/` compose files
- "Run RFS end-to-end in minikube": `deployment/k8s/` + see the
  README in that directory for the helm chart wiring.

## Spotless gotcha

Spotless 7.x requires a JDK-21 Gradle daemon. The repo pins spotless at
6.25.0 on purpose. Don't bump it casually. If CI fails with
`UnsupportedClassVersionError` on spotless, the daemon JVM is wrong, not
the plugin version.
