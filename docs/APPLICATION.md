# Enterprise Network Health & SLA — Application Guide

Medline Canada · Enterprise Network Reliability

This document explains what the application is, why it exists, how it works, and — page by
page, section by section — what every screen shows and what each number means.

---

## 1. What this application is (and why)

It is a **read-only reliability and SLA reporting portal** for Medline Canada's Cisco network
(distribution/access switches, routers, and Catalyst 9120/9130 access points), built on top of
**LogicMonitor** as the monitoring source of truth.

Two ideas shape everything:

1. **The portal never changes LogicMonitor.** Every call to LogicMonitor is a signed, read-only
   `GET`; adding, editing, or deleting a device changes only this application's local inventory.
2. **Evidence over guesses.** Availability is computed from real monitoring minutes and is
   *coverage-gated*: if there isn't enough evidence for a window, the app shows **"Insufficient
   evidence"** — never a fabricated `0%` or `100%`. Metrics LogicMonitor does not expose for this
   tenant (e.g. BGP, jitter) are labelled **"Not monitored"** rather than invented.

**Why:** leadership needs a defensible, single-pane view of network reliability — a real SLA number,
where the risk is, and where the network sits against industry resiliency tiers — without anyone
having to trust a hand-made spreadsheet.

---

## 2. How it works (architecture)

```
Browser ──> nginx ──> React UI  and  /api/v1 ──> FastAPI ──> PostgreSQL
                                                  │
                                                  └─ read-only LMv1 GETs ──> LogicMonitor
```

- **Frontend:** React + TypeScript (Vite), served by nginx. Dark/light "command-center" theme.
- **Backend:** FastAPI + SQLAlchemy. Owns validation, RBAC, LogicMonitor signing, collection,
  SLA math, resilience scoring, trends, and report generation.
- **Database:** PostgreSQL. Stores local inventory, per-collection **snapshots**, a daily SLA
  **rollup**, resilience assessments, and audit events.
- **Background jobs** (started at boot):
  - **Device refresh — every 30 min:** re-collects health/interfaces/neighbors/OSPF for **every**
    mapped Fleet device and writes a snapshot.
  - **SLA rollup — every 6 h:** recomputes recent days into the daily availability store.
  - **Resilience assessment — every 12 h:** recomputes the network tier estimate.
  - **AP status — every 2 h:** marks each access point Online/Offline from switch neighbor tables.
  - **SLA backfill — at first boot:** pulls availability history back to Jan 1 and stores it.

**Scope rule:** analytics only ever consider devices registered in **Device Fleet**, never the whole
LogicMonitor account.

---

## 3. Core concepts and formulas (what the numbers mean)

- **Availability** = `up eligible minutes / observed eligible minutes`. A minute is "up" when the
  device answered at least one ping (packet loss < 100%). Shown as a percentage (e.g. `99.982%`).
- **Coverage** = `observed minutes / expected minutes` for a window. Below **90%** the window is
  **Insufficient evidence** and no availability is published. Today's partial day counts only the
  minutes elapsed so far, so short windows aren't unfairly penalised.
- **Windows:** WTD (week-to-date, Monday→now), MTD, QTD, rolling-30, YTD — all in the configured
  timezone (America/Vancouver by default), all derived from the stored daily rollup.
- **Downtime minutes** (used in the trend chart) = `observed − up` minutes in a period.
- **Resilience tier** = a **network-redundancy estimate** mapped onto the Uptime Institute I–IV
  bands from observed redundancy (redundant uplinks, stack members, dual power) plus measured
  availability. **It is not a facility (power/cooling) certification.** The fleet tier is limited
  by its weakest critical node.
- **Incidents / MTTR / MTBF** = derived from availability history (contiguous below-100% days). They
  are approximate; exact start/stop timings would require LogicMonitor alert history.
- **Criticality** = the business importance you set per device (Critical / High / Standard).

**Semantic status words** you'll see everywhere and what they mean:
- **HEALTHY / OK / Target met** — good, with sufficient evidence.
- **DEGRADED / WARNING / At risk** — a real problem or a metric near its threshold.
- **CRITICAL** — a device down, unreachable, or in hardware/environment fault.
- **INSUFFICIENT EVIDENCE** — not enough monitoring data to judge (never treated as 0%).
- **NOT MONITORED / NOT AVAILABLE** — LogicMonitor does not provide this for the fleet.

---

## 4. The pages (what each screen shows)

Hover the **ⓘ** icon beside any section title in the app for a one-line reminder of these
explanations.

### 4.1 Overview — Executive Reliability Command Center
The leadership landing page. Everything is fleet-scoped and drawn from stored evidence (no live
LogicMonitor call on load), so it is fast and reconciles with the other pages.

- **Header status strip** — API status (LogicMonitor reachable), Database status, **Last sync**
  (age of the newest snapshot; "STALE" if old), **Fleet Coverage** (`monitored / total` devices),
  **LM Coverage** (`matched / total`), **SLA Evidence** (% coverage), **Data Quality**
  (High/Medium/Low from coverage + freshness).
- **Export buttons** — generate an **Executive Excel** workbook or **Executive PowerPoint** deck
  from the live numbers.
- **Global SLA Status** (ring) — 30-day fleet availability vs target, with the delta and status.
- **Device Criticality** (donut) — all Fleet devices by Critical/High/Standard; centre = fleet size.
- **7-Day Availability** (bars) — availability per day for the last week; red bars = below target.
- **Aggregate Throughput** — live aggregate interface traffic (Σ speed × utilisation over up
  interfaces). A gross fleet aggregate, not WAN egress.
- **Executive Summary** — an auto-generated narrative of the current reliability picture.
- **Core Infrastructure Health** — the devices most needing attention, ranked by problem severity ×
  criticality. Click a row to open its detail.
- **What leadership can act on now** — the top prioritised P1/P2 actions.
- **Site Reliability** — per-site availability (YTD & 30-day), device health, incidents, status.

### 4.2 Device Fleet — Fleet & SLA Compliance
The operational inventory of switches/routers.

- **Fleet Summary** — Total Network Devices (physical switches incl. stack members, APs excluded),
  Valid Uptime count, YTD availability, Below-SLA count, Monitoring Gaps, Collection Errors.
- **Compliance Trends** — the 12-week combo chart: **blue line** = weekly availability (left %
  axis), **orange bars** = weekly downtime minutes (right axis), **dashed amber line** = SLA target.
  Bars spike where the line dips. Also shows week-over-week and month-over-month direction.
- **Fleet Status** — each device with site, IP, current uptime, and WTD/YTD availability. Click a
  device name to open its full LogicMonitor detail.
- **Critical Applications** — business-application SLA; stays "Mapping pending" until an
  authoritative LogicMonitor application SLI is mapped (never inferred from device health).

### 4.3 Device detail (opened from Fleet)
Command-center view of one device: a **metric ribbon** (CPU, memory, uptime, OS), a **monitoring
state** badge, and tabs — **Health Data, Ping Quality, Interfaces, VLANs, CDP-LLDP Neighbors** (drawn
as a schematic topology: this switch at the hub, neighbors as named circles, each link labelled with
the switch-side port), **OSPF Neighbors, Inventory, Environmental & PoE, Alerts, Configuration
Backups, Monitoring Gaps, Collection Details**. Tables show "Not available from LogicMonitor" where a
datasource isn't mapped; missing data is never shown as zero.

### 4.4 Access Points
The Cisco 9120/9130 inventory. **Status is derived every 2 hours** by checking whether each AP's name
or MAC appears in the CDP/LLDP neighbor tables of *any* switch — present → **Online**, absent →
**Offline** — with the connected switch/interface and last-seen time.

### 4.5 SLA & Resilience
Governed SLA reporting and the resilience estimate.

- **KPI cards** — Fleet YTD availability, Fleet WTD availability, Below-target count, Estimated tier.
- **Uptime tier comparison** — measured availability against the Uptime Institute budgets
  (Tier I 99.671% → Tier IV 99.995%); the highlighted row is the estimated band.
- **Network resilience estimate** — the reasons behind the tier (uplinks, stack, dual power +
  measured availability), reassessed every 12 h; admins can trigger a fresh backfill + reassessment.
- **Per-device SLA** — WTD/YTD per device with YTD coverage and estimated tier (coverage-gated).
- **Trend intelligence & incidents** — WoW/MoM direction, availability-derived incidents, and
  approximate MTTR/MTBF, with a table of the largest downtime events.

### 4.6 Network Telemetry
Routing, interface, and latency detail — plus a transparency panel.

- **Data Coverage** — the honest matrix of what LogicMonitor exposes for this fleet: Monitored /
  Partial / Not monitored / Not available, per metric, with the datasource and device count.
- **Routing Health — OSPF** — OSPF adjacencies per device (Full/Total, neighbor events). BGP,
  EIGRP, and static routes are **Not monitored** (no instances exist in this tenant).
- **Interface / Circuit Health** — totals for up/down/high-utilisation/errors/flapping, and a
  worst-offenders issues table.
- **Latency & Loss** — average and worst-case round-trip latency and packet loss (from Ping).
  **Jitter is Not monitored** (the Cisco IP-SLA jitter datasource has no instances).

### 4.7 WAN Providers (isolated — not part of Medline metrics)
A deliberately **separate** page for the WAN providers' own routers (e.g. Centrilogic
circuits). These devices are **not** Medline's; they are collected read-only from
LogicMonitor for visibility only and are **never** included in the fleet SLA, Overview,
resilience, trends, or reports. They live in their own database table (`wan_routers`) so
they cannot leak into any company analytic.

- **Executive summary** — router count and health mix, average 24-hour reachability,
  established BGP peers, full OSPF adjacencies, and up interfaces. Descriptive counts
  only — no availability is scored against Medline.
- **By Provider** — routers grouped by the provider/circuit parsed from each device name.
- **Provider Routers** — every router with live status, reachability, BGP/OSPF/interface
  counts, and last sync. Open one for full detail.
- **Router detail** — a metric ribbon plus a **Routing Coverage** matrix (what LM really
  exposes for that router) and tabs: **BGP Peers, OSPF Neighbors, EIGRP Peers, IP Routing
  Stats, Interfaces, Inventory, Neighbors**. Empty datasources show "Not monitored";
  static routes are not monitored in this tenant.
- **Admin-only** add/remove (search LogicMonitor by name/IP and import; removal only
  detaches from this view — LogicMonitor is never modified). Refreshed every 30 minutes.

### 4.8 Settings (Administrators)
Device Inventory (add/import/map devices), Site Codes (with Business Unit), Device Naming (add/remove
Zones and Device Types with their role mappings — changes flow into the "Add device" pickers),
Appearance (theme), and other configuration.

---

## 5. What LogicMonitor provides for this fleet (discovered, not assumed)

| Area | Status |
|---|---|
| Availability / latency / loss (Ping, HostStatus) | **Monitored** |
| CPU, memory, temperature, fans, power, stack | **Monitored** |
| Interfaces (status, utilisation, errors) | **Monitored** |
| CDP/LLDP topology, config backups | **Monitored** |
| OSPF adjacency | **Partial** (only OSPF-enabled devices) |
| BGP, EIGRP, static routes, route-table counts | **Not monitored** (no instances / no datasource) |
| Jitter (Cisco IP-SLA) | **Not monitored** (no instances) |
| Aggregate WAN egress throughput | Not directly monitored (a gross interface aggregate is shown) |

The Data Coverage panel on **Network Telemetry** shows this live.

---

## 6. Security posture (summary)

- **Read-only to LogicMonitor:** the client rejects any non-GET locally; TLS is verified.
- **Authentication:** short-lived signed bearer tokens; passwords compared in constant time; login
  is rate-limited. Credentials load from Docker **secret files** in production, never the repo.
- **Authorization:** every endpoint is authenticated; all inventory/config writes and imports
  require an Administrator; collection requires Administrator or Network Engineer.
- **Injection-safe:** database access is parameterised; spreadsheet exports neutralise formula
  injection; report downloads validate the filename (no path traversal); uploads enforce size/type/
  row limits.
- **Hardening:** strict security headers (CSP `script-src 'self'`, `X-Frame-Options: DENY`,
  nosniff, no-referrer); CORS without credentials; containers run non-root with read-only
  filesystems; secrets are never returned in API responses or logs.
- **Production checklist:** rotate any exposed LM key, terminate TLS at the edge with OIDC/Entra,
  replace all default passwords, and run image/dependency scanners in CI.

---

## 7. Operating notes

- **Run:** `docker compose -f docker-compose.yml -f docker-compose.production.yml up -d --build`
  (the production override mounts the secret files; a plain `up` would fall back to defaults).
- **First boot** backfills SLA history to Jan 1 (one-time, throttled, resumable).
- **Reports** are generated on demand from the Overview export buttons and downloaded over the
  authenticated API.
- **Timezone/target/coverage** are configuration (`sla_timezone`, `sla_target`, `coverage_threshold`).
