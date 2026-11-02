# Stops FireVision AI: asks the supervisor that start.ps1 started to stop the Django server, the AI service (and the simulator),
# then checks that their ports are free (sentinel-django\tools\control.py). Only that supervisor is ever stopped.
# Usage (PowerShell, in the project folder):   .\stop.ps1        If scripts are blocked: powershell -ExecutionPolicy Bypass -File .\stop.ps1
$control = Join-Path $PSScriptRoot 'sentinel-django\tools\control.py'
$python = Join-Path $PSScriptRoot 'sentinel-django\.venv\Scripts\python.exe'
if (-not (Test-Path $control)) { Write-Host "sentinel-django\tools\control.py is missing: run stop.ps1 from the project folder." -ForegroundColor Red; exit 1 }
if (-not (Test-Path $python)) { Write-Host "sentinel-django\.venv is missing, so start.ps1 cannot have started anything: nothing to stop." -ForegroundColor Yellow; exit 0 }
& $python $control stop
exit $LASTEXITCODE
