<#
    build-release.ps1 - package this DEV machine's app as a release for the PRODUCTION machine.

    Run on THIS (dev) machine, from the repo root:
        .\scripts\build-release.ps1                       # code + database
        .\scripts\build-release.ps1 -NoDatabase           # code only (keep prod's own data)
        .\scripts\build-release.ps1 -Out D:\releases\sla  # custom output folder

    Produces a self-contained folder you copy to the production machine and restore with
    restore-release.ps1 (written into the same folder).

    What it does NOT do: it never touches the production machine, and it never puts real
    secrets in the package. Production keeps its own secrets/ files.
#>
[CmdletBinding()]
param(
    [string]$Out = "C:\EnterpriseNetworkSLA-Release",
    [switch]$NoDatabase,
    [string]$Tag = (Get-Date -Format "yyyy-MM-dd_HHmmss")
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$projectRoot = Split-Path -Parent $repo
$runtime = Join-Path $projectRoot 'docker'
if (-not (Test-Path "$runtime\docker-compose.yml")) {
    throw "Docker runtime not found at $runtime. Run releases only from the consolidated project layout."
}
Set-Location $runtime
function Info($m){ Write-Host "[release] $m" -ForegroundColor Cyan }
function Warn($m){ Write-Host "[release] $m" -ForegroundColor Yellow }

$compose = @('-f','docker-compose.yml','-f','docker-compose.production.yml')

Info "Building images from the current source..."
docker compose @compose build api frontend | Out-Null

# Fail fast if the working tree is dirty - a release should be reproducible from a commit.
$dirty = (git -C $repo status --porcelain 2>$null)
if ($dirty) { Warn "Working tree has uncommitted changes; this release will not match any commit." }
$commit = (git -C $repo rev-parse --short HEAD 2>$null)

New-Item -ItemType Directory -Force "$Out\images" | Out-Null
New-Item -ItemType Directory -Force "$Out\secrets" | Out-Null

# Retag -> save -> untag, so this machine's image list is left exactly as it was.
Info "Exporting images (renamed sla-network-*)..."
docker tag medline-enterprise-network-sla-api:1.2.0 sla-network-api:1.2.0
docker tag medline-enterprise-network-sla-web:1.2.0 sla-network-web:1.2.0
docker save sla-network-api:1.2.0 -o "$Out\images\api.tar"
docker save sla-network-web:1.2.0 -o "$Out\images\web.tar"
docker save postgres:16.9-alpine3.22 -o "$Out\images\postgres.tar"
docker rmi sla-network-api:1.2.0 sla-network-web:1.2.0 | Out-Null

if (-not $NoDatabase) {
    Info "Dumping the database (schema + data)..."
    docker compose @compose exec -T postgres `
        pg_dump -U network_sla -d network_sla --no-owner --clean --if-exists `
        > "$Out\db-backup.sql"
    $mb = [math]::Round((Get-Item "$Out\db-backup.sql").Length/1MB,1)
    Info "  db-backup.sql = $mb MB"
} else {
    Remove-Item "$Out\db-backup.sql" -ErrorAction SilentlyContinue
    Info "Skipped the database (-NoDatabase): production keeps its existing data."
}

Copy-Item "$repo\docker-compose.prod-release.yml" "$Out\docker-compose.yml" -Force
Copy-Item "$PSScriptRoot\restore-release.ps1"     "$Out\restore-release.ps1" -Force

# Secret TEMPLATES only - never the real dev secrets.
foreach ($n in 'jwt_secret','local_admin_password','local_user_password','lm_access_id','lm_access_key') {
    $f = "$Out\secrets\$n.txt"
    if (-not (Test-Path $f)) { Set-Content -NoNewline -Path $f -Value "REPLACE-ME" }
}

# .env template (never overwritten, so production keeps its own settings across releases).
if (-not (Test-Path "$Out\.env")) {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText("$Out\.env", @"
# Production settings for THIS machine. Reviewed once; later releases do not overwrite it.
POSTGRES_PASSWORD=change-me-strong-db-password
LM_PORTAL_URL=https://YOURPORTAL.logicmonitor.com
# Origin(s) the app is browsed from. Port 80 => no port suffix.
ALLOWED_ORIGINS=http://localhost
# 'production' ENFORCES the safety checks (rejects default credentials, disables API docs).
ENVIRONMENT=production
"@, $utf8)
    Info "Wrote .env template - review it on the production machine before restoring."
} else {
    Info "Kept the existing .env in the output folder."
}

@"
Release $Tag
Built from commit: $commit
Database included: $(if ($NoDatabase) {'no'} else {'yes'})
Images: sla-network-api:1.2.0, sla-network-web:1.2.0, postgres:16.9-alpine3.22

Restore on the PRODUCTION machine:
  1. Copy this whole folder over.
  2. Put the REAL production values in secrets\*.txt (they are placeholders here) and
     review .env  - set ALLOWED_ORIGINS to the production URL and ENVIRONMENT=production.
  3. Run:  .\restore-release.ps1
"@ | Set-Content "$Out\RELEASE.txt"

Info "Release ready: $Out"
Get-ChildItem $Out -Recurse -File | ForEach-Object {
    "{0,8:N1} MB  {1}" -f ($_.Length/1MB), $_.FullName.Substring($Out.Length+1)
}
