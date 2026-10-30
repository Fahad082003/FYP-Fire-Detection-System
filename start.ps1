# Starts FireVision AI: the Django server (API + dashboard on :5291) and the AI service, each in its own window.
# Usage (PowerShell, in the project folder):   .\start.ps1            add -Simulator for the fire demo
# If scripts are blocked:                      powershell -ExecutionPolicy Bypass -File .\start.ps1
# -Django is still accepted (older guides use it); Django is the only server now.
param([switch]$Simulator, [switch]$Django)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

$envFile = Join-Path $root '.env'
if (-not (Test-Path $envFile)) { Write-Host "No .env file. Copy .env.example to .env and fill it in first (see the setup guide)." -ForegroundColor Red; exit 1 }
# The AI service needs the server's AI device key; the simulator also needs the sensor gateway key.
function Read-EnvValue([string]$name) { (Get-Content $envFile | Where-Object { $_ -match "^\s*$name=" } | Select-Object -First 1) -replace "^\s*$name=", '' }
$key = Read-EnvValue 'Device__ApiKey'
if (-not $key) { Write-Host "Device__ApiKey is empty in .env: fill it in first." -ForegroundColor Red; exit 1 }
$sensorKey = Read-EnvValue 'Device__SensorApiKey'
if ($Simulator -and -not $sensorKey) { Write-Host "Device__SensorApiKey is empty in .env: the simulator needs it for sensor readings." -ForegroundColor Red; exit 1 }

function Open-Window([string]$title, [string]$folder, [string]$command) {
    $script = "`$host.UI.RawUI.WindowTitle = '$title'; Set-Location -LiteralPath '$folder'; $command"
    Start-Process powershell -ArgumentList '-NoExit', '-ExecutionPolicy', 'Bypass', '-Command', $script | Out-Null
    Write-Host "Started: $title"
}

$django = Join-Path $root 'sentinel-django'
if (-not (Test-Path (Join-Path $django '.venv\Scripts\python.exe'))) { Write-Host "sentinel-django\.venv is missing: see sentinel-django\README.md, Setup." -ForegroundColor Red; exit 1 }
Open-Window 'FireVision AI - Server' $django '.\.venv\Scripts\python -m uvicorn config.asgi:application --host 127.0.0.1 --port 5291'
$python = Join-Path $root 'ai-service\.venv\Scripts\python.exe'
if (Test-Path $python) {
    Open-Window 'FireVision AI - AI service' (Join-Path $root 'ai-service') "`$env:SENTINEL_DEVICE_KEY = '$key'; .\.venv\Scripts\python -m sentinel_ai"
} else {
    Write-Host "AI service skipped: ai-service\.venv is missing (see the setup guide, Step 4)." -ForegroundColor Yellow
}
if ($Simulator) {
    Open-Window 'FireVision AI - Simulator' $django "Start-Sleep 10; .\.venv\Scripts\python tools\simulator.py --device-key '$key' --sensor-key '$sensorKey' --ignite 'Ground Floor - Warehouse'"
}
Write-Host "`nOpen http://localhost:5291 in about 30 seconds. Close a window (or press Ctrl+C in it) to stop that part." -ForegroundColor Green
