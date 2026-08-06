# Deployment runbook

Create secret files outside the repository, reference them under Compose `secrets`, copy `.env.example` to `.env` for non-secret configuration, and run `docker compose up -d --build`. Verify `http://host:8080/api/v1/health`. Back up with `scripts/docker-backup.ps1` or `.sh`; restore only into a stopped, compatible database after validating the target and backup.

Upgrade: take a backup, pin the new image/tag, build or pull, run database migrations, restart, verify health and a synthetic collection, then retain the prior image for rollback. Reports and database data use named volumes.
