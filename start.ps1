# Starts FireVision AI under the supervisor (sentinel-django\tools\supervisor.py, in its own window): the Django server
# (API + dashboard on :5291) and the AI service. It waits until both are ready, or says why not (sentinel-django\tools\control.py).
# Usage (PowerShell, in the project folder):   .\start.ps1            add -Simulator for the fire demo.   Stop: .\stop.ps1
# If scripts are blocked:                      powershell -ExecutionPolicy Bypass -File .\start.ps1
# -Django is still accepted (older guides use it); Django is the only server now.
param([switch]$Simulator, [switch]$Django)
$control = Join-Path $PSScriptRoot 'sentinel-django\tools\control.py'
$python = Join-Path $PSScriptRoot 'sentinel-django\.venv\Scripts\python.exe'
if (-not (Test-Path $control)) { Write-Host "sentinel-django\tools\control.py is missing: run start.ps1 from the project folder." -ForegroundColor Red; exit 1 }
if (-not (Test-Path $python)) { Write-Host "sentinel-django\.venv is missing: see sentinel-django\README.md, Setup." -ForegroundColor Red; exit 1 }
& $python -c "pass"
if ($LASTEXITCODE -ne 0) { Write-Host "The Python that sentinel-django\.venv was made with is missing or broken: reinstall it, then recreate the venv (sentinel-django\README.md, Setup)." -ForegroundColor Red; exit 1 }
$arguments = @('start')
if ($Simulator) { $arguments += '--simulator' }
& $python $control @arguments
exit $LASTEXITCODE
