# First-time setup on a new PC: checks Python 3.13, both virtual environments and their packages, the ODBC driver, .env,
# SQL Server and the AI model files, and says what to do for each problem (sentinel-django\tools\setup_check.py).
# It asks before creating a venv, installing packages or creating .env from .env.example, and never downloads models,
# overwrites .env or touches the database. Run it again until everything is OK, then: .\start.ps1
# Usage (PowerShell, in the project folder):   .\setup.ps1
# If scripts are blocked:                      powershell -ExecutionPolicy Bypass -File .\setup.ps1
$checker = Join-Path $PSScriptRoot 'sentinel-django\tools\setup_check.py'
if (-not (Test-Path $checker)) { Write-Host "sentinel-django\tools\setup_check.py is missing: run setup.ps1 from the project folder." -ForegroundColor Red; exit 1 }
$python = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3.13 -c "pass" 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { $python = 'py'; $prefix = @('-3.13') }
}
if (-not $python -and (Get-Command python -ErrorAction SilentlyContinue)) {
    & python -c "import sys; sys.exit(sys.version_info[:2] != (3, 13))" 2>&1 | Out-Null  # also skips the Microsoft Store stub
    if ($LASTEXITCODE -eq 0) { $python = 'python'; $prefix = @() }
}
if (-not $python) {
    Write-Host "Python 3.13 was not found on this PC." -ForegroundColor Red
    Write-Host "Install Python 3.13 (64-bit) from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH' and 'py launcher'),"
    Write-Host "open a new PowerShell window and run .\setup.ps1 again."
    exit 1
}
& $python @prefix $checker
exit $LASTEXITCODE
