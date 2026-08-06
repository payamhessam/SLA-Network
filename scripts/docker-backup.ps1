$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path backups | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
docker compose exec -T postgres pg_dump -U network_sla -Fc network_sla > "backups/network-sla-$stamp.dump"
