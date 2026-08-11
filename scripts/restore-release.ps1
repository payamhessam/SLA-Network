<#
    restore-release.ps1 - restore a release onto the PRODUCTION machine.

    Run on the PRODUCTION machine, from inside the release folder (elevated PowerShell):
        .\restore-release.ps1                # load images, restore DB if empty, start
        .\restore-release.ps1 -ForceDatabase # OVERWRITE production data with the dump
        .\restore-release.ps1 -Down          # stop the stack (data volume kept)

    Safety: it will NOT overwrite an existing production database unless you explicitly
    pass -ForceDatabase, and it takes a safety dump of the current data before doing so.
#>
[CmdletBinding()]
param([switch]$ForceDatabase, [switch]$Down)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
function Info($m){ Write-Host "[restore] $m" -ForegroundColor Cyan }
function Warn($m){ Write-Host "[restore] $m" -ForegroundColor Yellow }
function Die ($m){ Write-Host "[restore] $m" -ForegroundColor Red; exit 1 }

try { docker version --format '{{.Server.Version}}' | Out-Null }
catch { Die "Docker is not running. Start Docker Desktop and re-run." }

if ($Down) { docker compose down; Info "Stopped (data volume kept)."; return }

foreach ($f in '.env','docker-compose.yml','images\api.tar','images\web.tar','images\postgres.tar') {
    if (-not (Test-Path $f)) { Die "Missing required file: $f" }
}

# --- refuse to deploy with placeholder secrets ---
$bad = @()
foreach ($n in 'jwt_secret','local_admin_password','local_user_password','lm_access_id','lm_access_key') {
    $v = (Get-Content "secrets\$n.txt" -Raw -ErrorAction SilentlyContinue)
    if (-not $v -or $v.Trim() -eq '' -or $v -match 'REPLACE-ME|CHANGE-ME|PASTE-') { $bad += $n }
}
if ($bad.Count) { Die "These secrets still hold placeholders: $($bad -join ', ')`nPut the real production values in secrets\ before restoring." }

Info "Loading images..."
docker load -i images\api.tar      | Out-Null
docker load -i images\web.tar      | Out-Null
docker load -i images\postgres.tar | Out-Null

Info "Starting database..."
docker compose up -d postgres
for ($i=0; $i -lt 40; $i++) {
    if ((docker inspect --format '{{.State.Health.Status}}' $(docker compose ps -q postgres) 2>$null) -eq 'healthy') { break }
    Start-Sleep -Seconds 2
}

if (Test-Path 'db-backup.sql') {
    $pg = docker compose ps -q postgres
    $hasData = $false
    try {
        $r = docker exec -i $pg psql -U network_sla -d network_sla -tAc "SELECT to_regclass('public.sla_daily') IS NOT NULL;" 2>$null
        if ($r -match 't') {
            $rows = docker exec -i $pg psql -U network_sla -d network_sla -tAc "SELECT count(*) FROM sla_daily;" 2>$null
            if ([int]($rows -replace '\s','') -gt 0) { $hasData = $true }
        }
    } catch {}

    if ($hasData -and -not $ForceDatabase) {
        Warn "Production already holds SLA history - NOT overwriting it."
        Warn "Re-run with -ForceDatabase only if you really intend to replace production data."
    } elseif ($hasData -and $ForceDatabase) {
        $safety = "pre-restore-$(Get-Date -Format yyyyMMdd_HHmmss).sql"
        Info "Taking a safety dump of current production data -> $safety"
        docker exec -i $pg pg_dump -U network_sla -d network_sla --no-owner --clean --if-exists > $safety
        Info "Restoring db-backup.sql (overwriting)..."
        Get-Content 'db-backup.sql' -Raw | docker exec -i $pg psql -U network_sla -d network_sla -v ON_ERROR_STOP=0 | Out-Null
    } else {
        Info "Database is empty - restoring db-backup.sql..."
        Get-Content 'db-backup.sql' -Raw | docker exec -i $pg psql -U network_sla -d network_sla -v ON_ERROR_STOP=0 | Out-Null
    }
} else {
    Info "No db-backup.sql in this release - leaving production data as-is."
}

Info "Starting API and web..."
docker compose up -d

Write-Host ""
Info "Done. Open the app and sign in as 'admin' with the password in secrets\local_admin_password.txt"
Info "Watch logs:  docker compose logs -f"
