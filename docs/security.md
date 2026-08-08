# Security and threat model

Trust boundaries are browser/edge, application/database, and application/LogicMonitor. Primary threats are stolen API secrets, forged sessions, unsafe imports, SSRF through a configurable portal URL, excessive API calls, report data leakage, and accidental write operations.

Controls include GET-only remote client enforcement, HTTPS-only LogicMonitor URLs, hostname validation, runtime secret files, redacted errors, hashed local passwords, signed expiring tokens, role checks, upload size/type limits, CSV formula neutralization on export, ORM parameterization, restricted CORS, secure headers, request IDs, rate limiting, audit logs, non-root containers, internal database networking, and no secrets in persistence or frontend bundles.

Production requirements: rotate the credentials exposed in chat; provision a LogicMonitor identity limited to read operations; use Docker/Kubernetes secrets or a managed vault; enable TLS; replace local authentication with OIDC/Entra; set unique high-entropy JWT/admin secrets; restrict ingress; back up encrypted volumes; review audit logs; and scan dependencies/images in CI.

## Production go-live checklist

The application enforces these when `ENVIRONMENT=production` is set (it refuses to start
otherwise; in development it only logs warnings). Verify readiness at any time as an
administrator via `GET /api/v1/readiness`.

1. **Set `ENVIRONMENT=production`** on the api service (enables the fail-fast guard below).
2. **Secrets** — provide via Docker/K8s secret files (already wired in
   `docker-compose.production.yml`): a unique ≥32-char `jwt_secret`, distinct high-entropy
   `local_admin_password` and `local_user_password`, and the read-only `lm_access_id` /
   `lm_access_key`. The startup guard rejects the development defaults.
3. **Rotate the LogicMonitor key** exposed during development; provision an LM identity
   limited to read operations.
4. **Database** — use PostgreSQL (not SQLite); back up the encrypted volume.
5. **CORS** — set `ALLOWED_ORIGINS` to the real edge origin(s); localhost is rejected in
   production.
6. **TLS + identity at the edge** — terminate TLS (HSTS is already emitted) and front the
   app with OIDC/Entra; restrict ingress to trusted networks.
7. **CI** — `.github/workflows/security.yml` runs the tests, `pip-audit`, `npm audit`, and
   Trivy image scans, failing on high/critical findings.
8. **Operations** — review audit logs; monitor `/api/v1/health` and `/api/v1/readiness`.

Security checks are documented in `docs/review-report.md`. This is a defensive review, not a certification or a substitute for an authorized independent penetration test.

