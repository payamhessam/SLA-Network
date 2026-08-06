param([Parameter(Mandatory=$true)][string]$Backup)
$ErrorActionPreference = 'Stop'
$resolved = Resolve-Path -LiteralPath $Backup
Write-Host "Restoring validated backup: $resolved"
Get-Content -Raw -LiteralPath $resolved | docker compose exec -T postgres pg_restore -U network_sla -d network_sla --clean --if-exists
