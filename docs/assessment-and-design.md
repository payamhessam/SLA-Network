# Assessment and design record

## Existing project assessment (2026-08-05)

The supplied workspace contained only empty `work/` and `outputs/` directories. The named XLSX, PPTX, CSV, Docker, and source artifacts were not supplied, so no workbook sheets or presentation layouts could be inspected or reused. The pasted master specification is the only design baseline. No obsolete code or existing Docker architecture was found. The production credentials pasted in the request are a security incident risk and must be rotated; they are not stored or exercised by this implementation.

## Architecture

Browser -> nginx -> React UI and `/api/v1` -> FastAPI -> PostgreSQL. A scheduler invokes the same read-only collection service. Generated reports use a persistent volume. Secrets enter at runtime through secret files or environment variables. FastAPI owns validation, RBAC, audit events, LogicMonitor signing, matching, collection, scoring, and reporting.

Core tables are users, devices, device_snapshots, collection_runs, audit_events, and settings. Device rows include type (`switch`, `router`, `access_point`), activation state, local metadata, and an optional read-only LogicMonitor mapping. Snapshots retain raw normalized values and explicit missing-data state.

Collection flow: select active local devices -> GET LogicMonitor resources with pagination -> deterministic match (IP, display name, hostname, FQDN, case-insensitive/short hostname) -> discover instances/data sources -> normalize known candidates -> calculate health only with fresh evidence -> persist atomically. Ambiguous resources are never selected automatically.

Authentication uses short-lived signed bearer tokens for the local development mode; production should terminate TLS at the edge and use OIDC/Entra. Roles are Administrator, Network Engineer, Leadership Viewer, and Read-Only Viewer. Administrative writes affect only local tables.

## LogicMonitor API plan

All remote calls are `GET`, signed with LMv1, paginated, rate-limited, retried only for safe transient failures, and made with TLS verification. Resource inventory uses `/santaba/rest/device/devices`; active alerts use `/santaba/rest/alert/alerts`; device data-source and instance discovery uses device datasource/instance resources; historical data uses instance data endpoints with explicit start/end epochs. Required permission is view/manage-read access only. Tenant DataSource names vary, so mappings are candidates rather than promises.

| Category | Candidate source | Window | Limitation/fallback |
|---|---|---:|---|
| Identity/model/serial/OS | device properties | current | Not available from LogicMonitor |
| Availability/ping/loss/latency | Ping or host status datasource | current, 24h, 7d | Not monitored |
| CPU/memory/uptime | Cisco CPU/memory/SNMP uptime instances | current, 1h, 24h | Mapping pending |
| Interfaces/errors/utilization | SNMP interface instances | current, 24h | Not collected from LogicMonitor |
| Environment/PoE | Cisco environment/PoE datasources | current | Not monitored |
| Alerts | alert resources | active, prior 24h | Collection failed |
| AP radios/clients | Cisco WLC/AP radio datasources | current, 24h | Not available from LogicMonitor |

## Cisco and AP field mapping

| Fleet field | Candidate datasource/datapoint | Endpoint class | Fallback |
|---|---|---|---|
| Model (incl. C9120/C9130) | system properties `system.model` / `auto.model` | device | Not available from LogicMonitor |
| Serial/IOS-XE | system properties, Cisco inventory | device/instance | Not available from LogicMonitor |
| Reachability/loss/latency | Ping `Status`, `PercentLoss`, `rtt` | instance data | Not monitored |
| CPU/memory | Cisco CPU/Memory utilization | instance data | Mapping pending |
| Uptime/reload | SNMP uptime/Cisco system | instance data | Not available from LogicMonitor |
| Interface state/util/errors/CRC | SNMP interfaces | instance data | Not collected from LogicMonitor |
| Temperature/fan/PSU/PoE | Cisco environment/PoE | instance data | Not monitored |
| AP client count | Cisco WLC AP, `clientCount` | instance data | Not monitored |
| AP radio/channel/utilization | Cisco AP Radio, `channel`, `channelUtilization` | instance data | Mapping pending |
| AP noise/SNR | Cisco AP Radio, `noiseFloor`, `snr` | instance data | Not monitored |

Models are classified as APs when model text matches `C?9120` or `C?9130`, or when the local type is `access_point`. AP metrics can be gathered from an AP resource or its controller depending on the tenant datasource topology.

## SLA model

Availability = eligible successful minutes / eligible observed minutes. Scheduled downtime is excluded only when evidence identifies it. Target defaults to 99.9%. WTD, MTD, quarter, rolling-30, and YTD use UTC observations displayed in the configured timezone. Coverage = observed eligible minutes / expected eligible minutes; below 90% produces `Insufficient evidence`, no ranking, and no fabricated zero. Confidence is High (>=98%), Medium (>=90%), or Insufficient. Unknown, stale, failed, and missing observations are reported separately.

## Docker plan and assumptions

Images: nginx frontend/proxy, non-root API, non-root scheduler, PostgreSQL 16. Ports: 8080 public; database is internal. Volumes persist database, reports, uploads, and logs. Health checks gate startup. Backup uses `pg_dump`; restore uses `psql`; upgrades require backup, image pull/build, migrations, then health verification.

Known gaps: actual tenant datasource names and permissions were not tested; referenced design artifacts were absent; production OIDC requires tenant registration; vulnerability scanners and Docker daemon may not be installed locally; SLA history begins only after collection. No claim is made that every requested metric exists in LogicMonitor.

