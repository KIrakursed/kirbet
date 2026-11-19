# Deploys the bot to a boxd machine and runs it 24/7 (systemd unit arb-bot).
#   powershell -ExecutionPolicy Bypass -File deploy\deploy_boxd.ps1 [-Machine telegrambot] [-Region fra]
# Stop the local copy of the bot first: two copies with one Telegram token conflict.
# The server's database (data/arb.db: users, settings, history) is never overwritten.
param([string]$Machine = "telegrambot", [string]$Region = "fra")
$ErrorActionPreference = "Stop"
$env:BOXD_TELEMETRY = "0"
$boxd = Join-Path $env:USERPROFILE ".local\bin\boxd.exe"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$existing = & $boxd machine list --region $Region --json | ConvertFrom-Json
$names = @($existing | ForEach-Object { $_.name })
if ($names -notcontains $Machine) {
    Write-Host "== creating machine $Machine in $Region (1 vCPU / 4 GB)"
    & $boxd machine new $Machine --region $Region --vcpu 1 --memory 4G --auto-suspend-timeout 0 --auto-hibernate-timeout 0
}
# the bot only talks outbound (Telegram polling, bookmaker pages): never let the machine sleep
& $boxd machine config set $Machine auto-suspend.timeout 0 --region $Region
& $boxd machine config set $Machine auto-hibernate.timeout 0 --region $Region

Write-Host "== packing project"
$tar = Join-Path $env:TEMP "arb-bot.tar.gz"
if (Test-Path $tar) { Remove-Item $tar -Force }
$tarExe = Join-Path $env:SystemRoot "System32\tar.exe"  # Windows bsdtar; GNU tar from Git Bash reads "C:" as a host
& $tarExe -czf $tar --exclude=.venv --exclude=.venv312 --exclude=__pycache__ --exclude=.pytest_cache --exclude=.ruff_cache `
    --exclude=data/bot.log --exclude=data/bot_run.log --exclude=data/research_kz.json --exclude=data/research_kz_terms.json `
    --exclude=data/bookmakers_live.json --exclude=data/arb.db --exclude=data/arb.db-journal `
    --exclude=data/proxy_stats.json --exclude=data/odds_api_credits.json `
    --exclude=app/providers/bookmakers/fonbet/tests/fixtures/fonbet_en_full.html `
    app config docs migrations deploy scripts alembic.ini requirements.txt pyproject.toml README.md .env.example data

Write-Host "== uploading"
& $boxd machine exec $Machine --region $Region -- mkdir -p bot
& $boxd machine cp $tar "${Machine}:bot/arb-bot.tar.gz" --region $Region
& $boxd machine exec $Machine --region $Region -- bash -lc "cd ~/bot && tar -xzf arb-bot.tar.gz && rm arb-bot.tar.gz"
# The server's .env is the source of truth for its secrets (DB password, ADMIN_PIN, ...): the local .env
# is uploaded only when the server has none yet, and is never overwritten afterwards.
$hasEnv = & $boxd machine exec $Machine --region $Region -- bash -lc "test -f ~/bot/.env && echo yes || echo no"
if ("$hasEnv".Trim() -ne "yes") {
    Write-Host "== first deploy: uploading .env"
    & $boxd machine cp .env "${Machine}:bot/.env" --region $Region
}
& $boxd machine exec $Machine --region $Region -- bash -lc "chmod 600 ~/bot/.env"

Write-Host "== installing and starting the service (several minutes)"
& $boxd machine exec $Machine --region $Region --timeout 1800 -- bash -lc "cd ~/bot && bash deploy/setup.sh"
Remove-Item $tar -Force
Write-Host "== logs:  boxd machine exec $Machine --region $Region -- sudo journalctl -u arb-bot -n 50 --no-pager"
