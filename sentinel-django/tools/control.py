"""What start.ps1 and stop.ps1 run (Windows). Usage, from the project folder:

    sentinel-django\\.venv\\Scripts\\python sentinel-django\\tools\\control.py start [--simulator]
    sentinel-django\\.venv\\Scripts\\python sentinel-django\\tools\\control.py stop

start: checks .env, the dependencies and the database (connect only, nothing is written), refuses a second start and a busy
port, launches tools\\supervisor.py in its own window and waits until Django answers and the AI service says "ok". It only
watches: restarts stay the supervisor's (its restart policy). Exit code 0 = ready (or already running).
stop: asks the supervisor recorded in run\\supervisor.json to stop (its run\\stop file) and checks the ports are free. That
supervisor counts only when its PID still has the recorded creation time; no other process is ever stopped.
Nothing printed here contains a key or password: log lines shown are redacted. Standard library only.
"""
import ctypes
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import supervisor as sv

DJANGO_READY_SECONDS = 90
AI_READY_SECONDS = 300  # model loading and warm-up on a slow CPU
STOP_WAIT_SECONDS = 45  # the supervisor gives each child 10 s, then terminates it
PORT_WAIT_SECONDS = 10
DB_CHECK_SECONDS = 60
WINDOW = 1  # SW_SHOWNORMAL: the supervisor window; tests hide it
TAIL_LINES = 12
DJANGO_PACKAGES = ("django", "rest_framework", "mssql", "pyodbc", "uvicorn")
AI_PACKAGES = ("numpy", "cv2", "onnxruntime")
# a name ending in one of these words, then = or : and the value (KeyError: stays readable)
SECRET = re.compile(r"(?i)(\w*(?:password|pwd|key|token|secret)\s*[:=]\s*)[^\s;,'\"]+")
PROCESS_TERMINATE = 0x0001
sv._kernel32.TerminateProcess.argtypes = (sv.wintypes.HANDLE, sv.wintypes.UINT)

# Connects with the server's own settings and prints only the error type: driver messages can name servers and users.
DB_CHECK = """
import os, sys
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
try:
    django.setup()
    from django.db import connection
except Exception as e:
    sys.exit(f"configuration error ({type(e).__name__}: {e})")
try:
    connection.ensure_connection()
except Exception as e:
    sys.exit(f"cannot connect ({type(e).__name__})")
"""


def say(message: str) -> None:
    print(message, flush=True)


def env_secrets(root: Path = sv.ROOT) -> list[str]:
    """The .env values long enough to be a key or password, only to hide them in log lines shown here."""
    try:
        lines = (root / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    values = (line.split("=", 1)[1].strip().strip('"') for line in lines if "=" in line and not line.lstrip().startswith("#"))
    return sorted({v for v in values if len(v) >= 8}, key=len, reverse=True)


def redact(text: str, secrets=()) -> str:
    for secret in secrets:
        text = text.replace(secret, "***")
    return SECRET.sub(r"\1***", text)


def tail(path, secrets=()) -> str:
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[-TAIL_LINES:]
    except OSError:
        return "    (no log yet)"
    return redact("\n".join(f"    {line}" for line in lines), secrets)


def missing_packages(python: Path, packages) -> list[str]:
    code = "import importlib.util as u, sys; print(' '.join(n for n in sys.argv[1:] if u.find_spec(n) is None))"
    out = subprocess.run([str(python), "-c", code, *packages], capture_output=True, text=True, timeout=60)
    return out.stdout.split() if out.returncode == 0 else [f"(python failed: exit code {out.returncode})"]


def preflight(root: Path = sv.ROOT, secrets=()) -> str | None:
    """Why the start must not go ahead, from the checks that need nothing to run; None when they all pass."""
    if os.environ.get("SENTINEL_NO_DOTENV") != "1" and not (root / ".env").is_file():
        return "No .env file in the project folder. Copy .env.example to .env and fill it in first (see the setup guide)."
    missing = [n for n in DJANGO_PACKAGES if importlib.util.find_spec(n) is None]
    if missing:
        return (f"Dependencies missing in sentinel-django\\.venv: {', '.join(missing)}. Install them: "
                "sentinel-django\\.venv\\Scripts\\python -m pip install -r sentinel-django\\requirements.txt")
    ai_python = root / "ai-service" / ".venv" / "Scripts" / "python.exe"
    if ai_python.is_file() and (missing := missing_packages(ai_python, AI_PACKAGES)):
        return (f"Dependencies missing in ai-service\\.venv: {', '.join(missing)}. Install them: "
                "ai-service\\.venv\\Scripts\\python -m pip install -r ai-service\\requirements.txt")
    try:
        db = subprocess.run([sys.executable, "-c", DB_CHECK], cwd=root / "sentinel-django", capture_output=True, text=True,
                            timeout=DB_CHECK_SECONDS)
    except subprocess.TimeoutExpired:
        return f"Database unavailable: no answer within {DB_CHECK_SECONDS} s. Is SQL Server running?"
    if db.returncode:
        reason = redact(db.stderr.strip().splitlines()[-1] if db.stderr.strip() else f"exit code {db.returncode}", secrets)
        return f"Database unavailable: {reason}. Is SQL Server running and ConnectionStrings__Sentinel in .env correct?"
    return None


def running_supervisor(run_dir: Path) -> dict | None:
    """The supervisor recorded in the state file, only while that very process (PID + creation time) runs."""
    state = sv.read_state(Path(run_dir) / "supervisor.json")
    return state if sv.same_process(state.get("supervisor", {})) else None


def terminate(entry: dict) -> bool:
    """Ends that very process: its creation time is checked on the same handle that ends it, so a reused PID is never hit."""
    handle = sv._kernel32.OpenProcess(PROCESS_TERMINATE | sv.PROCESS_QUERY_LIMITED_INFORMATION, False, entry["pid"])
    if not handle:
        return False
    try:
        times = [sv.wintypes.FILETIME() for _ in range(4)]
        if not sv._kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)) \
                or times[0].dwHighDateTime << 32 | times[0].dwLowDateTime != entry["start"]:
            return False
        return bool(sv._kernel32.TerminateProcess(handle, 1))
    finally:
        sv._kernel32.CloseHandle(handle)


def wait_ports_free(ports, seconds=PORT_WAIT_SECONDS) -> list[int]:
    end = time.monotonic() + seconds
    while (busy := [p for p in ports if not sv.port_free(p)]) and time.monotonic() < end:
        time.sleep(0.2)
    return busy


def stop(run_dir: Path, wait: float = STOP_WAIT_SECONDS) -> int:
    run_dir = Path(run_dir)
    state = running_supervisor(run_dir)
    if not state:
        say("Sentinel is not running (no supervisor to stop). Nothing was stopped.")
        return 0
    entry = state["supervisor"]
    ports = [c["port"] for c in state.get("children", {}).values() if c.get("port")]
    say(f"Stopping Sentinel (supervisor PID {entry['pid']}): Django and the AI service get Ctrl+Break and up to 10 s to close...")
    (run_dir / "stop").touch()
    end = time.monotonic() + wait
    while sv.same_process(entry) and time.monotonic() < end:
        time.sleep(0.2)
    if sv.same_process(entry):
        # The Windows job object ends its children with it.
        say(f"The supervisor did not stop within {wait:g} s: ended it" if terminate(entry) else "ERROR: the supervisor could not be ended")
        time.sleep(1)
    busy = wait_ports_free(ports)
    if busy:
        say(f"ERROR: port(s) {busy} still in use after the stop. Find the owner with: netstat -ano | findstr :<port>")
        return 1
    say(f"Sentinel stopped. Ports {ports} are free." if ports else "Sentinel stopped.")
    return 0


def _models(entry: dict) -> list[dict]:
    try:
        with urllib.request.urlopen(entry["healthUrl"], timeout=sv.HEALTH_TIMEOUT_SECONDS) as r:
            models = json.loads(r.read()).get("models")
        return models if isinstance(models, list) else []
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return []


def start(launch: list[str], run_dir: Path, log_dir: Path, ports: list[int], django_seconds: float = DJANGO_READY_SECONDS,
          ai_seconds: float = AI_READY_SECONDS, secrets=()) -> int:
    """launch: the command that runs the supervisor. Returns once Django and the AI service are ready, or why not."""
    run_dir, log_dir = Path(run_dir), Path(log_dir)
    if state := running_supervisor(run_dir):
        say(f"Sentinel is already running (supervisor PID {state['supervisor']['pid']}); nothing was started. "
            "Stop it first with .\\stop.ps1.")
        return 0
    busy = [p for p in ports if not sv.port_free(p)]
    if busy:
        say(f"ERROR: port(s) {busy} already in use by another program; nothing was started. Close it (netstat -ano | findstr "
            ":<port> shows its PID) and start again.")
        return 1
    launcher = subprocess.Popen(launch, creationflags=subprocess.CREATE_NEW_CONSOLE,
                                startupinfo=subprocess.STARTUPINFO(dwFlags=subprocess.STARTF_USESHOWWINDOW, wShowWindow=WINDOW))
    say("Supervisor started in its own window. Waiting for Django...")

    def fail(message: str, log: Path, stop_all: bool) -> int:
        say(f"ERROR: {message}\n  Last lines of {log}:\n{tail(log, secrets)}")
        if stop_all:
            stop(run_dir)
        else:
            say("Sentinel keeps running (the dashboard shows it). Fix the cause, then .\\stop.ps1 and .\\start.ps1.")
        return 1

    django_end, ai_end, reported = time.monotonic() + django_seconds, None, None
    while True:
        time.sleep(0.5)
        state = running_supervisor(run_dir)
        if not state:
            if launcher.poll() is not None:
                return fail(f"the supervisor exited (code {launcher.returncode}) before Sentinel was ready",
                            log_dir / "supervisor.log", False)
            if time.monotonic() > django_end:
                return fail("the supervisor did not report in time", log_dir / "supervisor.log", False)
            continue
        django, ai = state["children"].get("django", {}), state["children"].get("ai")
        if ai_end is None:
            if django.get("state") == "exited":
                return fail(f"Django failed to start (exit code {django.get('exitCode')})", log_dir / "django.log", False)
            answer = sv.health_status(f"http://127.0.0.1:{django['port']}/health") if django.get("state") == "running" else None
            if answer != "ok":
                if time.monotonic() > django_end:
                    return fail(f"Django did not answer /health within {django_seconds:g} s (last: {answer})",
                                log_dir / "django.log", True)
                continue
            say(f"Django ready (http://localhost:{django['port']}).")
            if ai is None:
                say("WARNING: the AI service was not started (ai-service\\.venv is missing): no fire or smoke detection. "
                    f"Dashboard: http://localhost:{django['port']}")
                return 0
            say("Waiting for the AI service to load and warm up its models (it is not ready while it says loading)...")
            ai_end = time.monotonic() + ai_seconds
        if django.get("state") == "exited":
            return fail(f"Django stopped (exit code {django.get('exitCode')})", log_dir / "django.log", True)
        if ai["state"] == "ready":
            loaded = [m.get("name") for m in _models(ai) if m.get("loaded")]
            say(f"AI service ready: models loaded and warmed up: {', '.join(map(str, loaded)) or '(not listed)'}.")
            say(f"\nSentinel is running. Dashboard: http://localhost:{django['port']}   Stop: .\\stop.ps1")
            return 0
        if ai["state"] == "failed":
            return fail(f"the AI service failed to start: it exited {ai['restarts'] + 1} times (last exit code {ai['exitCode']}) "
                        "and the supervisor stopped restarting it", Path(ai["log"]), False)
        if ai["health"] in ("no-model", "degraded"):
            models = _models(ai)
            broken = "; ".join(f"{m.get('name')}: {m.get('error')}" for m in models if not m.get("loaded")) or "no model loaded"
            what = "no model could be loaded, nothing can be detected" if ai["health"] == "no-model" else "some models failed"
            return fail(f"AI service started but {what} ({redact(broken, secrets)}). Check ai-service\\models.", Path(ai["log"]), False)
        if ai["state"] == "backoff" and reported != ai["restarts"]:
            reported = ai["restarts"]
            say(f"AI service exited (code {ai['exitCode']}); the supervisor restarts it in {ai['backoffSeconds']:g} s.")
        if time.monotonic() > ai_end:
            return fail(f"AI service not ready within {ai_seconds:g} s (last /health: {ai['health']})", Path(ai["log"]), False)


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else ""
    if command == "stop":
        return stop(sv.ROOT / "run")
    if command != "start":
        say(__doc__)
        return 2
    simulator = "--simulator" in argv
    secrets = env_secrets()
    problem = preflight(secrets=secrets)
    if problem:
        say(f"ERROR: {problem}")
        return 1
    try:
        children = sv.default_children(simulator)  # the supervisor's own checks (keys, restart policy), here where they show
    except SystemExit as e:
        say(f"ERROR: {e}")
        return 1
    launch = [sys.executable, str(Path(sv.__file__))] + (["--simulator"] if simulator else [])
    return start(launch, sv.ROOT / "run", sv.ROOT / "logs", [c["port"] for c in children if c.get("port")], secrets=secrets)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
