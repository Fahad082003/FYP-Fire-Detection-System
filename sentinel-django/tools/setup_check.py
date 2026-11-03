"""First-time setup check for a new Windows PC: what setup.ps1 runs, with the Python 3.13 it found. Usage, from the project folder:

    py -3.13 sentinel-django\\tools\\setup_check.py

Checks, in order, and says what to do for each problem: Python 3.13; sentinel-django\\.venv and ai-service\\.venv with their
packages (offers to create a venv or pip-install requirements.txt, only after a "y"); the ODBC driver; .env (offers to copy
.env.example when there is none, never overwrites one, never fills in or prints a value); SQL Server and SentinelDb (connect
and list tables only: nothing is created, restored or written); the model files against models.json (sha256; never
downloaded here). Exit code 0 = ready for .\\start.ps1. Standard library only.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import control
import supervisor as sv

PYTHON = (3, 13)
VENVS = {"sentinel-django": control.DJANGO_PACKAGES, "ai-service": control.AI_PACKAGES}
ODBC = "ODBC Driver 18 for SQL Server"
INSTALL_PYTHON = ("Install Python 3.13 (64-bit) from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH' and "
                  "'py launcher'), open a new PowerShell window and run .\\setup.ps1 again.")
# Read-only: the same connection test start.ps1 makes, then whether the Sentinel tables exist.
DB_CHECK = control.DB_CHECK + 'print("tables" if "CameraFeeds" in connection.introspection.table_names() else "empty")\n'


def ask(question: str) -> bool:
    """Yes only for an explicit y/yes typed by a person; no console (a script, a test) = no."""
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def check_python(version=sys.version_info) -> str | None:
    if tuple(version[:2]) != PYTHON:
        return f"This is Python {version[0]}.{version[1]}; Sentinel needs Python 3.13. {INSTALL_PYTHON}"
    return None


def check_venv(root: Path, name: str, packages, ask=ask) -> str | None:
    venv, requirements = root / name / ".venv", root / name / "requirements.txt"
    python = venv / "Scripts" / "python.exe"
    if not python.is_file():
        if not ask(f"{name}\\.venv is missing. Create it now with this Python {PYTHON[0]}.{PYTHON[1]}? (a local folder, no download)"):
            return f"{name}\\.venv is missing. Create it: py -3.13 -m venv {name}\\.venv   (or run .\\setup.ps1 and answer y)"
        if subprocess.run([sys.executable, "-m", "venv", str(venv)]).returncode or not python.is_file():
            return f"Could not create {name}\\.venv (see the error above). Check the disk space and that the folder is writable."
    found = subprocess.run([str(python), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"], capture_output=True, text=True)
    if found.returncode or found.stdout.strip() != f"{PYTHON[0]}.{PYTHON[1]}":
        what = f"Python {found.stdout.strip()}" if found.returncode == 0 else "a Python that no longer runs"
        return f"{name}\\.venv was made with {what}, not 3.13. Delete the {name}\\.venv folder and run .\\setup.ps1 again."
    missing = control.missing_packages(python, packages)
    if missing:
        install = f"{name}\\.venv\\Scripts\\python -m pip install -r {name}\\requirements.txt"
        if not ask(f"{name}: packages missing ({', '.join(missing)}). Install {name}\\requirements.txt now? (downloads from the internet)"):
            return f"{name}: packages missing ({', '.join(missing)}). Install them: {install}"
        subprocess.run([str(python), "-m", "pip", "install", "-r", str(requirements)])  # pip's own progress and errors are shown
        if missing := control.missing_packages(python, packages):
            return f"{name}: still missing after pip ({', '.join(missing)}): read pip's error above (internet connection?), then: {install}"
    return None


def check_odbc(root: Path) -> str | None:
    wanted = sv.env_value("SENTINEL_ODBC_DRIVER") or ODBC
    out = subprocess.run([str(root / "sentinel-django" / ".venv" / "Scripts" / "python.exe"), "-c",
                          "import pyodbc; print('\\n'.join(pyodbc.drivers()))"], capture_output=True, text=True)
    if wanted not in out.stdout.splitlines():
        return (f"'{wanted}' is not installed. Install the Microsoft ODBC Driver 18 for SQL Server (x64): "
                "https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server")
    return None


def env_values(root: Path, ask=ask) -> tuple:
    """The settings the server will read: .env, or the environment with SENTINEL_NO_DOTENV=1 (as config/settings.py does)."""
    if os.environ.get("SENTINEL_NO_DOTENV") == "1":
        return os.environ, None  # not dict(): on Windows its keys are upper-cased, os.environ ignores case
    path, example = root / ".env", root / ".env.example"
    if not path.is_file():
        if not example.is_file() or not ask("There is no .env. Copy .env.example to .env now? (you fill in the values yourself)"):
            return None, "No .env file in the project folder. Copy-Item .env.example .env, then fill in the values listed below."
        with open(example, "rb") as src, open(path, "xb") as dst:  # "x": never replaces a .env that appeared meanwhile
            shutil.copyfileobj(src, dst)
        control.say(f"Created {path.name} from .env.example. Open it in Notepad and fill in the values listed below.")
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"')
    return values, None


def check_env(values: dict) -> list[str]:
    """Only what the server needs to start; names are shown, never values."""
    v = {k: values.get(k, "") for k in ("Jwt__SigningKey", "Device__ApiKey", "Device__SensorApiKey")}
    problems = []
    if len(v["Jwt__SigningKey"]) < 32:
        problems.append("Jwt__SigningKey must be at least 32 characters of random text. Make one in PowerShell: "
                        "[Convert]::ToBase64String((1..48 | % {Get-Random -Max 256}))")
    if not v["Device__ApiKey"]:
        problems.append("Device__ApiKey is empty: the AI service needs it. Use another random text (same command as above).")
    if v["Device__SensorApiKey"] and v["Device__SensorApiKey"] == v["Device__ApiKey"]:
        problems.append("Device__SensorApiKey must differ from Device__ApiKey (or stay empty: sensor gateways disabled).")
    return problems


def check_database(root: Path, secrets=()) -> str | None:
    restore = ("To use the database from the other PC: git pull, then .\\database\\load.ps1 (it OVERWRITES a local SentinelDb; "
               "read it first). Only for a brand-new empty database: sentinel-django\\.venv\\Scripts\\python sentinel-django\\manage.py "
               "create_schema (and set Seed__AdminPassword in .env first: it becomes the first admin's password)")
    try:
        db = subprocess.run([str(root / "sentinel-django" / ".venv" / "Scripts" / "python.exe"), "-c", DB_CHECK],
                            cwd=root / "sentinel-django", capture_output=True, text=True, timeout=control.DB_CHECK_SECONDS)
    except subprocess.TimeoutExpired:
        db = None
    if db is None or db.returncode:
        reason = (f"no answer within {control.DB_CHECK_SECONDS} s" if db is None else
                  control.redact(db.stderr.strip().splitlines()[-1] if db.stderr.strip() else f"exit code {db.returncode}", secrets))
        return (f"Database unavailable ({reason}). 1) Is SQL Server installed and running? (Win+R, services.msc, 'SQL Server "
                f"(MSSQLSERVER)', Start.) 2) Is ConnectionStrings__Sentinel in .env right for this PC (instance name, LocalDB)? "
                f"3) Does SentinelDb exist here? {restore}")
    if db.stdout.strip().splitlines()[-1:] != ["tables"]:
        return f"SentinelDb has no Sentinel tables. {restore}"
    return None


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_models(root: Path) -> list[str]:
    models = root / "ai-service" / "models"
    problems = []
    for spec in json.loads((models / "models.json").read_text(encoding="utf-8"))["models"]:
        path = models / spec["file"]
        get = (f"download it (from {spec['url'].split('/')[2]}, sha256-checked): ai-service\\.venv\\Scripts\\python "
               "ai-service\\tools\\download_models.py" if spec.get("url") else
               f"it is not in git and has no download: copy ai-service\\models\\{spec['file']} from the other PC "
               "(or train it: ai-service\\training\\README.md)")
        if not path.is_file():
            problems.append(f"Model {spec['name']}: {spec['file']} is missing; {get}")
        elif sha256(path) != spec["sha256"]:
            problems.append(f"Model {spec['name']}: {spec['file']} is not the expected file (sha256 mismatch, damaged or another "
                            f"version); replace it: {get}. Do not edit models.json.")
    return problems


def main(root: Path = sv.ROOT, ask=ask) -> int:
    problems = []

    def report(name, found):
        found = [found] if isinstance(found, str) else found or []
        control.say(f"[{'FIX' if found else 'OK '}] {name}")
        for p in found:
            control.say(f"      {p}")
        problems.extend(found)
        return not found

    if not report(f"Python {PYTHON[0]}.{PYTHON[1]}", check_python()):
        return 1
    ready = {name: report(f"{name}\\.venv and packages", check_venv(root, name, packages, ask)) for name, packages in VENVS.items()}
    values, missing_env = env_values(root, ask)
    env_ok = report(".env (required settings)", missing_env or check_env(values))
    if ready["sentinel-django"]:
        if report("ODBC driver for SQL Server", check_odbc(root)) and env_ok:
            report("SQL Server and SentinelDb (read-only check)", check_database(root, control.env_secrets(root)))
        elif env_ok:
            control.say("[-- ] SQL Server check skipped: install the ODBC driver first")
    else:
        control.say("[-- ] ODBC driver and SQL Server checks skipped: sentinel-django\\.venv is not ready")
    report("AI model files (sha256 from models.json)", check_models(root))
    if problems:
        control.say(f"\n{len(problems)} thing(s) to fix (above). Then run .\\setup.ps1 again.")
        return 1
    control.say("\nSetup complete. Start Sentinel: .\\start.ps1   (it waits until Django and the AI models are ready)   Stop: .\\stop.ps1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
