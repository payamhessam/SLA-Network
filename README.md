# Enterprise Network Health and SLA

Read-only LogicMonitor reporting for Cisco network devices, including Catalyst 9120/9130 access points. The portal keeps its own device inventory; adding or removing a device never changes LogicMonitor.

## Quick start

1. Copy `.env.example` to `.env` and set fresh LogicMonitor credentials. Prefer Docker secret files in production.
2. Run `docker compose up --build`.
3. Open `http://localhost:8080`. Development login: `admin` / the value of `LOCAL_ADMIN_PASSWORD`.

The application builds dedicated project images named `medline-enterprise-network-sla-api:1.2.0` and `medline-enterprise-network-sla-web:1.2.0`. It does not reuse application images from other projects. See `CHANGELOG.md` for the version history.

Credentials pasted into chat should be rotated. They are intentionally absent from this repository.

## Local development

```powershell
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
$env:LOCAL_ADMIN_PASSWORD='a-long-development-password'
.venv\Scripts\uvicorn app.main:app --reload
```

The OpenAPI document is at `/docs`. See `docs/assessment-and-design.md`, `docs/security.md`, and `docs/deployment.md`.
