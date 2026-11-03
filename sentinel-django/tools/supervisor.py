"""Runs the Django server and the AI service as child processes this supervisor owns (Windows).

Usage: .\\start.ps1 starts it (through tools\\control.py) and waits until it is ready; .\\stop.ps1 stops it. Directly, from the
project folder:  sentinel-django\\.venv\\Scripts\\python sentinel-django\\tools\\supervisor.py [--simulator]
Stop: .\\stop.ps1, Ctrl+C or Ctrl+Break in its window, or create the file run\\stop. Logs: logs\\<name>.log. State: run\\supervisor.json.

Safety rules:
  * one supervisor at a time: an OS file lock, released by Windows even when the process is killed;
  * the supervisor and everything it starts share a Windows job object that ends them all when the supervisor ends, even when
    it is killed (no orphans); without that job nothing is started;
  * a state file left by an earlier run is checked, never trusted: a PID counts as that child only when the process with
    that PID has the recorded creation time (PID reuse), and even then it is reported, never stopped;
  * a busy port refuses the start, so a running server is never replaced;
  * only processes started here are stopped (through their own handles, which also pins the PID).
The AI service is restarted when it exits or hangs (bounded, with backoff; both share one budget) and counts as ready only when
its /health says "ok". Hung = alive but /health gets no answer for several checks in a row AND a set time (see hang_policy).
Standard library only.
"""
import ctypes
import http.client
import json
import logging
import math
import msvcrt
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DJANGO, AI = ROOT / "sentinel-django", ROOT / "ai-service"
DJANGO_PORT = 5291
CHECK_SECONDS = 1
STOP_SECONDS = 10  # graceful stop wait before a child is terminated
HEALTH_TIMEOUT_SECONDS = 2
# shortcut: rotated only when a process (re)starts, one old file kept; a single very long run can still grow a log, add a
# size check to the loop if that happens
LOG_MAX_BYTES = 10 * 1024 * 1024
# name -> (environment variable, default); see restart_policy()
POLICY = {"max_restarts": ("Supervisor__MaxRestarts", 5), "window": ("Supervisor__RestartWindowSeconds", 600),
          "initial": ("Supervisor__InitialBackoffSeconds", 2), "max_backoff": ("Supervisor__MaxBackoffSeconds", 60)}
# name -> (environment variable, default); see hang_policy()
HANG = {"interval": ("Supervisor__HealthIntervalSeconds", 5), "timeout": ("Supervisor__HealthTimeoutSeconds", 3),
        "checks": ("Supervisor__HangFailedChecks", 3), "seconds": ("Supervisor__HangSeconds", 30),
        "cooldown": ("Supervisor__RecoveryCooldownSeconds", 60)}
NO_ANSWER = "no answer"  # health_status() text when nothing answered: the only result that can count towards a hang

log = logging.getLogger("supervisor")

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
_kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
_kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
_kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE = 0x1000, 259
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 9, 0x2000


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def join_kill_on_close_job() -> int:
    """Puts this process in a new job that Windows ends, with every process in it, when its last handle closes. Only this
    process holds the handle, so a crash or kill of the supervisor ends its children too. Processes started from now on join
    the job automatically (the venv launcher and the interpreter it starts), so there is no window in which one escapes."""
    job = _kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not _kernel32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)) \
            or not _kernel32.AssignProcessToJobObject(job, _kernel32.GetCurrentProcess()):
        error = ctypes.WinError(ctypes.get_last_error())
        _kernel32.CloseHandle(job)
        raise error
    return job  # never closed: it must live exactly as long as this process


def process_start(pid: int) -> int | None:
    """Creation time (FILETIME ticks) of the running process with this PID, or None when none runs (or it is not visible)."""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        code, times = wintypes.DWORD(), [wintypes.FILETIME() for _ in range(4)]
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return None  # an exited process whose object is still held open by someone
        if not _kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        return times[0].dwHighDateTime << 32 | times[0].dwLowDateTime
    finally:
        _kernel32.CloseHandle(handle)


def same_process(entry: dict) -> bool:
    """True only when entry's PID is running AND is the very process recorded (same creation time)."""
    pid, start = entry.get("pid"), entry.get("start")
    return isinstance(pid, int) and isinstance(start, int) and process_start(pid) == start


def port_free(port: int) -> bool:
    """Nothing answers on it and it can be bound. Both are needed on Windows: a server on 0.0.0.0 does not block a 127.0.0.1
    bind (measured), and a bound socket that is not listening does not answer."""
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            return False
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _numbers(table: dict, environ, whole: str, zero_ok: bool) -> dict:
    """table's settings from the environment; an invalid value stops the start with the reason. `whole`: the one key that must
    be a whole number, and the only one that may be 0 when zero_ok."""
    values = {}
    for key, (name, default) in table.items():
        raw = environ.get(name, "").strip()
        try:
            value = float(raw) if raw else float(default)
        except ValueError:
            raise SystemExit(f"{name}={raw!r} is not a number")
        may_be_zero = zero_ok and key == whole
        if not math.isfinite(value) or value < 0 or (not may_be_zero and value == 0):
            raise SystemExit(f"{name}={raw!r} must be {'0 or more' if may_be_zero else 'more than 0'}")
        if key == whole and value != int(value):
            raise SystemExit(f"{name}={raw!r} must be a whole number")
        values[key] = int(value) if key == whole else value
    return values


def restart_policy(environ=os.environ) -> dict:
    """The AI restart limits from Supervisor__* variables; an invalid value stops the start with the reason."""
    policy = _numbers(POLICY, environ, "max_restarts", zero_ok=True)
    if policy["initial"] > policy["max_backoff"]:
        raise SystemExit("Supervisor__InitialBackoffSeconds must not be more than Supervisor__MaxBackoffSeconds")
    return policy


def hang_policy(environ=os.environ) -> dict:
    """When an AI that stops answering /health counts as hung: `checks` failed checks in a row (one every `interval` s, each
    waiting `timeout` s) AND no answer for `seconds`, and never within `cooldown` s of a (re)start, so one slow answer, a short
    network delay or a slow model load is not a hang. Loading, degraded or no-model answers are answers: never a hang."""
    hang = _numbers(HANG, environ, "checks", zero_ok=False)
    if hang["timeout"] > hang["interval"]:
        raise SystemExit("Supervisor__HealthTimeoutSeconds must not be more than Supervisor__HealthIntervalSeconds")
    return hang


def restart_delay(restarts: list[float], failures: int, now: float, policy: dict) -> float | None:
    """Seconds to wait before the next restart, or None once max_restarts restarts happened within the window.
    restarts: earlier restart times; failures: exits since the child was last ready (doubles the wait)."""
    if sum(now - t < policy["window"] for t in restarts) >= policy["max_restarts"]:
        return None
    return min(policy["max_backoff"], policy["initial"] * 2 ** failures)


def health_status(url: str, timeout: float = HEALTH_TIMEOUT_SECONDS) -> str:
    """The "status" of a /health answer, or why there is none. Only "ok" means ready."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            raw = r.read()
    except (OSError, ValueError, http.client.HTTPException) as e:  # refused, timed out, HTTP error, cut-off or garbled reply
        return f"{NO_ANSWER} ({type(e).__name__})"
    try:
        status = json.loads(raw).get("status")
    except (ValueError, AttributeError):
        status = None
    return status if isinstance(status, str) else "no status in /health"


def write_state(path: Path, state: dict) -> None:
    """Atomic: readers see the old or the new file, never half of one."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # a reader has the file open for a moment (Windows)
            if attempt == 19:
                raise
            time.sleep(0.05)


def read_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.warning("state file %s is unreadable; treated as stale", path)
        return {}


def rotate(path: Path) -> None:
    """A log over LOG_MAX_BYTES becomes <name>.1 (replacing the older one) before it is opened again."""
    try:
        if path.stat().st_size > LOG_MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:  # no log yet, or still open in another process (Windows): keep appending
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Supervisor:
    def __init__(self, children: list[dict], run_dir: Path, log_dir: Path):
        """children, started in this order: {"name", "cmd": [...], "cwd", "env": {...}, "port": int, "health": url,
        "restart": restart_policy(), "hang": hang_policy()}; all but name, cmd and cwd are optional (no "hang": never ended for
        not answering)."""
        self.specs = {s["name"]: s for s in children}
        self.run_dir, self.log_dir = Path(run_dir), Path(log_dir)
        self.state_path, self.stop_path = self.run_dir / "supervisor.json", self.run_dir / "stop"
        self.procs: dict[str, subprocess.Popen] = {}
        self.restarts: dict[str, list[float]] = {name: [] for name in self.specs}  # monotonic times
        self.failures: dict[str, int] = {name: 0 for name in self.specs}
        self.next_start: dict[str, float] = {}
        now = time.monotonic()
        self.next_check: dict[str, float] = {name: now for name in self.specs}
        self.failed_checks: dict[str, int] = {name: 0 for name in self.specs}  # /health without an answer, in a row
        self.answered_at: dict[str, float] = {name: now for name in self.specs}  # last answer, or the (re)start
        self.spawned_at: dict[str, float] = {name: now for name in self.specs}
        self.state = {"supervisor": {"pid": os.getpid(), "start": process_start(os.getpid())}, "children": {}}
        self.stopping = False
        self._lock = None

    def refusal(self) -> str | None:
        """Why this start must not go ahead (another supervisor, a child of an earlier run still alive, a busy port)."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._lock = open(self.run_dir / "supervisor.lock", "a+")
        try:
            msvcrt.locking(self._lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            other = read_state(self.state_path).get("supervisor", {}).get("pid", "?")
            self._lock.close()
            self._lock = None
            return f"a supervisor is already running (PID {other}); nothing was started"
        # The lock proves the supervisor that wrote this file is gone; its children may not be.
        for name, entry in read_state(self.state_path).get("children", {}).items():
            if isinstance(entry, dict) and same_process(entry):
                return (f"{name} from an earlier run is still running (PID {entry['pid']}); it was not touched. "
                        "Stop it first, then start again.")
        busy = [f"{s['name']} port {s['port']}" for s in self.specs.values() if s.get("port") and not port_free(s["port"])]
        if busy:
            return f"port in use: {', '.join(busy)} (another server is running?); nothing was started"
        try:
            join_kill_on_close_job()
        except OSError as e:
            return f"cannot create the Windows job object that ends the children with this supervisor ({e}); nothing was started"
        return None

    def spawn(self, name: str) -> None:
        spec = self.specs[name]
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / f"{name}.log"
        rotate(path)
        with path.open("a", encoding="utf-8") as out:
            out.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} started by supervisor PID {os.getpid()}\n")
            out.flush()
            # Own process group: Ctrl+Break reaches only this child. Unbuffered: the log shows output as it happens.
            proc = subprocess.Popen(spec["cmd"], cwd=spec["cwd"], stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    env={**os.environ, "PYTHONUNBUFFERED": "1", **spec.get("env", {})},
                                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        self.procs[name] = proc
        entry = self.state["children"].setdefault(name, {"port": spec.get("port"), "healthUrl": spec.get("health"),
                                                         "log": str(path), "restarts": 0,
                                                         "exitCode": None, "lastExitAt": None, "lastRestartAt": None,
                                                         "lastReadyAt": None, "backoffSeconds": None, "nextRestartAt": None,
                                                         "lastFailure": None, "unresponsiveSince": None})
        entry.update(pid=proc.pid, start=process_start(proc.pid), state="starting" if spec.get("health") else "running",
                     health=None, backoffSeconds=None, nextRestartAt=None, unresponsiveSince=None)
        self.spawned_at[name] = self.answered_at[name] = self.next_check[name] = time.monotonic()
        self.failed_checks[name] = 0
        log.info("started %s (PID %s)", name, proc.pid)

    def restart(self, name: str) -> None:
        entry = self.state["children"][name]
        self.restarts[name].append(time.monotonic())
        entry.update(restarts=entry["restarts"] + 1, lastRestartAt=_now_iso())
        log.info("restarting %s (restart %s)", name, entry["restarts"])
        self.spawn(name)

    def on_exit(self, name: str, code: int, failure: str | None = None) -> None:
        """failure: what happened, for the dashboard ("hung (...)"); a plain exit by default."""
        spec, entry = self.specs[name], self.state["children"][name]
        entry.update(exitCode=code, lastExitAt=_now_iso(), health=None, lastFailure=failure or f"exited (code {code})")
        log.warning("%s exited with code %s (see %s)", name, code, entry["log"])
        policy = spec.get("restart")
        if not policy:
            entry["state"] = "exited"
            return
        delay = restart_delay(self.restarts[name], self.failures[name], time.monotonic(), policy)
        self.failures[name] += 1
        if delay is None:
            entry["state"] = "failed"
            log.error("%s: not restarted again, it already restarted %s times within %s s. Read %s, fix the cause, then start "
                      "the supervisor again.", name, policy["max_restarts"], f"{policy['window']:g}", entry["log"])
            return
        self.next_start[name] = time.monotonic() + delay
        entry.update(state="backoff", backoffSeconds=delay,
                     nextRestartAt=datetime.fromtimestamp(time.time() + delay, timezone.utc).isoformat())
        log.info("%s will restart in %s s", name, f"{delay:g}")

    def check_ready(self, name: str) -> None:
        spec, entry, now = self.specs[name], self.state["children"][name], time.monotonic()
        hang = spec.get("hang")
        status = health_status(spec["health"], hang["timeout"]) if hang else health_status(spec["health"])
        ready, answered = status == "ok", not status.startswith(NO_ANSWER)
        if ready:
            entry["lastReadyAt"] = _now_iso()
            self.failures[name] = 0  # a healthy run: the next crash starts again from the shortest wait
        if status != entry["health"]:
            (log.info if ready else log.warning)("%s %s (/health: %s)", name, "is ready" if ready else "is not ready", status)
        if answered:  # loading, degraded and no-model are answers too: slow or limited, not hung
            self.failed_checks[name], self.answered_at[name] = 0, now
            state = "ready" if ready else "starting" if entry["state"] == "starting" else "not-ready"
            entry["unresponsiveSince"] = None
        else:
            self.failed_checks[name] += 1
            state = "starting" if entry["state"] == "starting" else "unresponsive"
            entry["unresponsiveSince"] = entry.get("unresponsiveSince") or _now_iso()
        entry.update(health=status, state=state)
        silent = now - self.answered_at[name]
        if hang and self.failed_checks[name] >= hang["checks"] and silent >= hang["seconds"] \
                and now - self.spawned_at[name] >= hang["cooldown"]:
            self.recover(name, f"{self.failed_checks[name]} health checks in a row got no answer, none for {silent:.0f} s "
                               f"(last: {status})")

    def recover(self, name: str, evidence: str) -> None:
        """A hung child: ended through the handle it was started with (only if that is still the process recorded at its
        start), then handled like a crash, so a hang and a crash use the same restart budget and backoff.
        shortcut: only the started process is terminated; the venv launcher ends the interpreter it runs (its own job object)
        and the supervisor's job ends anything else at the latest when the supervisor stops; track the tree if a child ever
        starts long-lived processes of its own."""
        proc, entry = self.procs[name], self.state["children"][name]
        log.error("%s is hung: %s. PID %s, log %s", name, evidence, proc.pid, entry["log"])
        if proc.poll() is not None or process_start(proc.pid) != entry["start"]:
            log.warning("%s: PID %s is no longer the process started here: not ended (its exit is handled next)", name, proc.pid)
            return
        proc.kill()
        proc.wait()
        log.warning("%s (PID %s) ended for not answering", name, proc.pid)
        self.on_exit(name, proc.returncode, f"hung ({evidence})")

    def check_children(self) -> None:
        for name, proc in list(self.procs.items()):
            entry = self.state["children"][name]
            code = proc.poll()
            if entry["state"] == "backoff":
                if time.monotonic() >= self.next_start[name]:
                    self.restart(name)
            elif code is not None:
                if entry["state"] not in ("exited", "failed"):
                    self.on_exit(name, code)
            elif self.specs[name].get("health") and time.monotonic() >= self.next_check[name]:
                hang = self.specs[name].get("hang")  # while starting, every second: ready is seen at once
                self.next_check[name] = time.monotonic() + (hang["interval"] if hang and entry["state"] != "starting" else CHECK_SECONDS)
                self.check_ready(name)

    def save(self) -> None:
        self.state["heartbeat"] = _now_iso()
        write_state(self.state_path, self.state)

    def stop_children(self) -> None:
        """Ctrl+Break to each running child (uvicorn shuts down cleanly on it), then terminate what is left."""
        running = [(n, p) for n, p in reversed(self.procs.items()) if p.poll() is None]
        for name, proc in running:
            try:
                os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
            except OSError as e:  # no console to send it through: terminate below
                log.warning("Ctrl+Break to %s failed: %s", name, e)
        deadline = time.monotonic() + STOP_SECONDS
        for name, proc in running:
            try:
                proc.wait(max(0.1, deadline - time.monotonic()))
                log.info("%s stopped (code %s)", name, proc.returncode)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                log.warning("%s did not stop within %s s: terminated", name, STOP_SECONDS)
            self.state["children"][name].update(state="stopped", exitCode=proc.returncode)

    def request_stop(self, *_):
        self.stopping = True

    def run(self) -> int:
        reason = self.refusal()
        if reason:
            log.error("refused: %s", reason)
            return 2
        try:
            self.stop_path.unlink(missing_ok=True)  # a stop request left from an earlier run
            log.info("supervisor started (PID %s)", os.getpid())
            signal.signal(signal.SIGBREAK, self.request_stop)
            try:
                for name in self.specs:
                    self.spawn(name)
            except OSError as e:  # e.g. a missing program: stop what already started, leave nothing behind
                log.error("start failed: %s", e)
                return 1
            while not self.stopping and not self.stop_path.exists():
                self.check_children()
                self.save()
                time.sleep(CHECK_SECONDS)
            return 0
        except KeyboardInterrupt:
            return 0
        finally:
            self.stop_children()
            self.state_path.unlink(missing_ok=True)
            self.stop_path.unlink(missing_ok=True)
            log.info("supervisor stopped")
            self._lock.close()  # releases the lock


def env_value(name: str) -> str:
    """From the environment, else from the project .env (as start.ps1 reads it); never logged."""
    if os.environ.get(name) or os.environ.get("SENTINEL_NO_DOTENV") == "1":
        return os.environ.get(name, "")
    try:
        lines = (ROOT / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    return next((l.split("=", 1)[1].strip().strip('"') for l in lines if l.strip().startswith(f"{name}=")), "")


def ai_listen() -> tuple[str, int]:
    config = AI / "config.json"
    listen = json.loads(config.read_text(encoding="utf-8")).get("listen", "127.0.0.1:8008") if config.is_file() else "127.0.0.1:8008"
    host, port = listen.rsplit(":", 1)
    return ("127.0.0.1" if host in ("", "0.0.0.0") else host), int(port)


def default_children(simulator: bool = False) -> list[dict]:
    """Django, the AI service when its venv exists, and the fire-demo simulator only on request. Keys reach the children through
    their environment, never a command line. Django gets no health URL: polled every second, it would add an access-log line
    each time (control.py checks it once at start)."""
    policy, hang = restart_policy(), hang_policy()  # checked first: a bad value stops here, before anything runs
    python = DJANGO / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        raise SystemExit("sentinel-django\\.venv is missing: see sentinel-django\\README.md, Setup.")
    children = [{"name": "django", "cwd": str(DJANGO), "port": DJANGO_PORT,
                 "cmd": [str(python), "-m", "uvicorn", "config.asgi:application", "--host", "127.0.0.1", "--port", str(DJANGO_PORT)]}]
    key = env_value("Device__ApiKey")
    ai_python = AI / ".venv" / "Scripts" / "python.exe"
    if not ai_python.is_file():
        log.warning("AI service skipped: ai-service\\.venv is missing (see the setup guide)")
    elif not key:
        raise SystemExit("Device__ApiKey is empty in .env: fill it in first.")
    else:
        host, port = ai_listen()
        children.append({"name": "ai", "cwd": str(AI), "port": port, "cmd": [str(ai_python), "-m", "sentinel_ai"],
                         "env": {"SENTINEL_DEVICE_KEY": key}, "health": f"http://{host}:{port}/health", "restart": policy,
                         "hang": hang})
    if simulator:
        sensor_key = env_value("Device__SensorApiKey")
        if not key or not sensor_key:
            raise SystemExit("The simulator needs Device__ApiKey and Device__SensorApiKey in .env: fill them in first.")
        # It exits while the server does not answer yet; the restart policy starts it again once Django is up.
        children.append({"name": "simulator", "cwd": str(DJANGO), "restart": policy,
                         "cmd": [str(python), str(DJANGO / "tools" / "simulator.py"), "--ignite", "Ground Floor - Warehouse"],
                         "env": {"Device__ApiKey": key, "Device__SensorApiKey": sensor_key}})
    return children


def setup_logging(log_dir: Path) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    rotate(Path(log_dir) / "supervisor.log")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(Path(log_dir) / "supervisor.log", encoding="utf-8")])


if __name__ == "__main__":
    _kernel32.SetConsoleTitleW("FireVision AI - Supervisor")
    setup_logging(ROOT / "logs")
    sys.exit(Supervisor(default_children("--simulator" in sys.argv), ROOT / "run", ROOT / "logs").run())
