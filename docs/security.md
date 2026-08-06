# Security and threat model

Trust boundaries are browser/edge, application/database, and application/LogicMonitor. Primary threats are stolen API secrets, forged sessions, unsafe imports, SSRF through a configurable portal URL, excessive API calls, report data leakage, and accidental write operations.

Controls include GET-only remote client enforcement, HTTPS-only LogicMonitor URLs, hostname validation, runtime secret files, redacted errors, hashed local passwords, signed expiring tokens, role checks, upload size/type limits, CSV formula neutralization on export, ORM parameterization, restricted CORS, secure headers, request IDs, rate limiting, audit logs, non-root containers, internal database networking, and no secrets in persistence or frontend bundles.

Production requirements: rotate the credentials exposed in chat; provision a LogicMonitor identity limited to read operations; use Docker/Kubernetes secrets or a managed vault; enable TLS; replace local authentication with OIDC/Entra; set unique high-entropy JWT/admin secrets; restrict ingress; back up encrypted volumes; review audit logs; and scan dependencies/images in CI.

Security checks are documented in `docs/review-report.md`. This is a defensive review, not a certification or a substitute for an authorized independent penetration test.

