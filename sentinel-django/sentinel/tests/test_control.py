"""tools/control.py (what start.ps1 and stop.ps1 run) with real Windows processes: the real supervisor with fake Django and AI
children, hidden windows, temp folders, free ports. Nothing here starts the real Django server, the AI service or touches a
database (the database check is pointed at a closed port); no process the test did not start is ever stopped."""
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from .test_supervisor import HEALTH_CHILD, LAUNCHER, TOOLS, child, free_port, health_child

import control  # noqa: E402  (tools/ is on sys.path through test_supervisor)
import supervisor  # noqa: E402

FAST = {"max_restarts": 2, "window": 60, "initial": 0.3, "max_backoff": 0.6}
# argv: port, seconds of "loading", final status, marker file. The first run exits with code 3 (a crash), later runs serve.
CRASH_ONCE = """
import os, sys
marker = sys.argv.pop(4)  # HEALTH_CHILD's own optional argv[4] stays free
if not os.path.exists(marker):
    open(marker, "w").close()
    print("crashing once", flush=True)
    sys.exit(3)
""" + HEALTH_CHILD
# A process that writes a supervisor state file naming itself and then ignores the stop request (a hung supervisor).
HUNG = """
import json, os, sys, time
sys.path.insert(0, sys.argv[2])
import supervisor
state = {"supervisor": {"pid": os.getpid(), "start": supervisor.process_start(os.getpid())}, "children": {}}
supervisor.write_state(supervisor.Path(sys.argv[1]), state)
time.sleep(60)
"""


def django(port, loading=0.3):
    """Like the real Django child: no health URL for the supervisor; control.py asks its /health itself."""
    spec = health_child("django", port, loading=loading, restart=None)
    del spec["health"]
    return spec


@unittest.skipUnless(os.name == "nt", "the supervisor is Windows-only")
class ControlTests(SimpleTestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="sentinel-control-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.run_dir, self.log_dir = self.dir / "run", self.dir / "logs"
        patcher = mock.patch.object(control, "WINDOW", 0)  # hidden supervisor window
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.quiet_stop)  # a failed test leaves nothing running

    def quiet_stop(self):
        with redirect_stdout(io.StringIO()):
            control.stop(self.run_dir)

    def launch(self, children):
        return [sys.executable, "-c", LAUNCHER, json.dumps(children), str(self.run_dir), str(self.log_dir), "10", "", str(TOOLS)]

    def start(self, children, **kwargs):
        out = io.StringIO()
        with redirect_stdout(out):
            code = control.start(self.launch(children), self.run_dir, self.log_dir, [c["port"] for c in children if c.get("port")],
                                 **{"django_seconds": 30, "ai_seconds": 30, **kwargs})
        return code, out.getvalue()

    def stop(self, **kwargs):
        out = io.StringIO()
        with redirect_stdout(out):
            code = control.stop(self.run_dir, **kwargs)
        return code, out.getvalue()

    def two(self, **ai):
        self.django_port, self.ai_port = free_port(), free_port()
        return [django(self.django_port), health_child("ai", self.ai_port, **{"loading": 0.5, "restart": FAST, **ai})]

    def test_normal_start_waits_for_loading_then_ready_and_stop_frees_the_ports(self):
        code, out = self.start(self.two(loading=2))
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"Django ready \(http://localhost:\d+\)[\s\S]*not ready while it says loading[\s\S]*AI service ready"
                              r"[\s\S]*Sentinel is running\. Dashboard: http://localhost:\d+")
        state = control.running_supervisor(self.run_dir)
        self.assertEqual(state["children"]["ai"]["state"], "ready")  # loading was not taken as ready
        self.assertIsNotNone(state["children"]["ai"]["lastReadyAt"])
        children = [state["children"][n] for n in ("django", "ai")]

        code, out = self.stop()
        self.assertEqual(code, 0, out)
        self.assertIn(f"Sentinel stopped. Ports [{self.django_port}, {self.ai_port}] are free.", out)
        self.assertFalse(any(map(supervisor.same_process, children + [state["supervisor"]])))
        for port in (self.django_port, self.ai_port):
            self.assertTrue(supervisor.port_free(port))
        self.assertFalse((self.run_dir / "supervisor.json").exists())

    def test_start_twice_and_stop_twice(self):
        self.assertEqual(self.start(self.two())[0], 0)
        first = control.running_supervisor(self.run_dir)
        code, out = self.start(self.two())
        self.assertEqual(code, 0)
        self.assertIn(f"Sentinel is already running (supervisor PID {first['supervisor']['pid']}); nothing was started", out)
        now = control.running_supervisor(self.run_dir)
        self.assertEqual(now["supervisor"], first["supervisor"])  # the first one is untouched
        self.assertTrue(supervisor.same_process(first["children"]["ai"]))

        self.assertEqual(self.stop()[0], 0)
        code, out = self.stop()
        self.assertEqual(code, 0)
        self.assertIn("Sentinel is not running (no supervisor to stop). Nothing was stopped.", out)

    def test_a_busy_port_refuses_the_start_and_the_holder_keeps_running(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            port = server.getsockname()[1]
            code, out = self.start([django(port)])
            self.assertEqual(code, 1)
            self.assertIn(f"port(s) [{port}] already in use by another program; nothing was started", out)
            self.assertFalse(self.log_dir.exists())  # no supervisor was launched
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                pass

    def test_django_startup_failure_is_shown_redacted_and_everything_stops(self):
        port, ai_port = free_port(), free_port()
        crash = {"name": "django", "cwd": str(TOOLS), "port": port,
                 "cmd": [sys.executable, "-c", "import sys; print('Created admin with GENERATED password: s3cr3t-Value9'); "
                                               "print('cannot import settings'); sys.exit(4)"]}
        code, out = self.start([crash, health_child("ai", ai_port, loading=0, restart=FAST)], secrets=["never-shown-0815"])
        self.assertEqual(code, 1, out)
        self.assertIn("ERROR: Django failed to start (exit code 4)", out)
        self.assertIn("cannot import settings", out)  # the actual reason, from logs\django.log
        self.assertNotIn("s3cr3t-Value9", out)
        self.assertIn("password: ***", out)
        self.assertIn("Sentinel stopped.", out)
        self.assertIsNone(control.running_supervisor(self.run_dir))
        self.assertTrue(supervisor.port_free(ai_port))

    def test_ai_startup_failure_after_the_restart_limit(self):
        port = free_port()
        crash = child("ai", seconds=0.2, code=3, port=free_port(), restart=FAST, health=f"http://127.0.0.1:{free_port()}/health")
        code, out = self.start([django(port), crash])
        self.assertEqual(code, 1, out)
        self.assertIn("ERROR: the AI service failed to start: it exited 3 times (last exit code 3) and the supervisor stopped "
                      "restarting it", out)
        self.assertIn("child up", out)  # the tail of logs\ai.log
        self.assertIn("Sentinel keeps running", out)  # the dashboard still answers and shows the AI as offline
        self.assertEqual(self.stop()[0], 0)

    def test_an_ai_crash_during_start_is_restarted_by_the_supervisor(self):
        port, ai_port, marker = free_port(), free_port(), self.dir / "crashed-once"
        ai = {"name": "ai", "cwd": str(TOOLS), "port": ai_port, "health": f"http://127.0.0.1:{ai_port}/health", "restart": FAST,
              "cmd": [sys.executable, "-c", CRASH_ONCE, str(ai_port), "0.5", "ok", str(marker)]}
        code, out = self.start([django(port), ai])
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"AI service exited \(code 3\); the supervisor restarts it in 0\.3 s\.[\s\S]*AI service ready")
        self.assertEqual(control.running_supervisor(self.run_dir)["children"]["ai"]["restarts"], 1)

    def test_ai_without_models_is_a_clear_error_and_never_ready(self):
        code, out = self.start(self.two(loading=0.5, final="no-model"))
        self.assertEqual(code, 1, out)
        self.assertIn("ERROR: AI service started but no model could be loaded, nothing can be detected", out)
        self.assertNotIn("AI service ready", out)
        self.assertEqual(control.running_supervisor(self.run_dir)["children"]["ai"]["lastReadyAt"], None)

    def test_ai_readiness_timeout(self):
        code, out = self.start(self.two(loading=60), ai_seconds=2)
        self.assertEqual(code, 1, out)
        self.assertIn("ERROR: AI service not ready within 2 s (last /health: loading)", out)

    def test_a_killed_supervisor_takes_its_children_and_the_next_start_works(self):
        self.assertEqual(self.start(self.two())[0], 0)
        state = control.running_supervisor(self.run_dir)
        os.kill(state["supervisor"]["pid"], signal.SIGTERM)  # a crash (identity checked by running_supervisor just above)
        children = [state["children"][n] for n in ("django", "ai")]
        end = time.monotonic() + 10
        while any(map(supervisor.same_process, children)) and time.monotonic() < end:
            time.sleep(0.1)
        self.assertFalse(any(map(supervisor.same_process, children)))  # the job object ended them
        code, out = self.stop()
        self.assertIn("Sentinel is not running", out)  # the state file left behind is not trusted
        self.assertEqual(self.start(self.two())[0], 0)

    def test_a_hung_supervisor_is_ended_after_the_wait_and_an_unrelated_process_is_never_touched(self):
        self.run_dir.mkdir(parents=True)
        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(bystander.wait)
        self.addCleanup(bystander.kill)  # this test's own process, through its own handle
        real = {"pid": bystander.pid, "start": supervisor.process_start(bystander.pid)}
        state_path = self.run_dir / "supervisor.json"
        # Its PID with another creation time: a reused PID. Not a supervisor; nothing is stopped.
        state_path.write_text(json.dumps({"supervisor": {**real, "start": real["start"] + 1}, "children": {"ai": real}}))
        code, out = self.stop(wait=1)
        self.assertIn("Sentinel is not running", out)
        self.assertTrue(supervisor.same_process(real))
        self.assertFalse(control.terminate({**real, "start": real["start"] + 1}))  # the identity check on the handle itself
        self.assertTrue(supervisor.same_process(real))

        hung = subprocess.Popen([sys.executable, "-c", HUNG, str(state_path), str(TOOLS)], creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(hung.wait)
        entry = self.wait_running()
        code, out = self.stop(wait=1)
        self.assertEqual(code, 0, out)
        self.assertIn("The supervisor did not stop within 1 s: ended it", out)
        self.assertEqual(hung.wait(10), 1)
        self.assertIsNone(supervisor.process_start(entry["pid"]))
        self.assertTrue(supervisor.same_process(real))

    def wait_running(self):
        end = time.monotonic() + 20
        while time.monotonic() < end:
            if state := control.running_supervisor(self.run_dir):
                return state["supervisor"]
            time.sleep(0.05)
        self.fail("no supervisor state")


class PreflightTests(SimpleTestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="sentinel-preflight-"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_missing_env_file(self):
        with mock.patch.dict(os.environ, {"SENTINEL_NO_DOTENV": ""}):
            self.assertIn("No .env file in the project folder", control.preflight(self.dir))

    def test_missing_dependencies(self):
        (self.dir / ".env").write_text("")
        real = control.importlib.util.find_spec
        with mock.patch.object(control.importlib.util, "find_spec", lambda n: None if n == "uvicorn" else real(n)):
            self.assertIn("Dependencies missing in sentinel-django\\.venv: uvicorn", control.preflight(self.dir))
        ai = self.dir / "ai-service" / ".venv" / "Scripts"
        ai.mkdir(parents=True)
        shutil.copy(sys.executable, ai / "python.exe")  # a venv launcher without its pyvenv.cfg: a broken AI python
        self.assertIn("Dependencies missing in ai-service\\.venv: (python failed: exit code", control.preflight(self.dir))
        shutil.copy(Path(sys.executable).parents[1] / "pyvenv.cfg", ai.parent)  # an empty venv: no numpy, cv2 or onnxruntime
        problem = control.preflight(self.dir)
        self.assertIn("Dependencies missing in ai-service\\.venv: numpy, cv2, onnxruntime", problem)

    def test_database_unavailable_without_driver_text(self):
        env = {"SENTINEL_NO_DOTENV": "1", "Jwt__SigningKey": "test-only-signing-key-0123456789abcdef",
               "ConnectionStrings__Sentinel": "Server=127.0.0.1,1;Database=test_unreachable;User Id=u;Password=Pw-never-shown-42;"
                                              "TrustServerCertificate=True;Connect Timeout=3"}
        with mock.patch.dict(os.environ, env):
            problem = control.preflight(control.sv.ROOT, secrets=["Pw-never-shown-42"])
        self.assertRegex(problem, r"^Database unavailable: cannot connect \(\w+\)\. Is SQL Server running")
        self.assertNotIn("Pw-never-shown-42", problem)

    def test_redaction(self):
        text = "Device__ApiKey=abc123secret token: xyz PWD=p4ss; KeyError: 'cameraId' value-long-secret"
        self.assertEqual(control.redact(text, ["value-long-secret"]), "Device__ApiKey=*** token: *** PWD=***; KeyError: 'cameraId' ***")


class ChildrenTests(SimpleTestCase):
    KEYS = {"Device__ApiKey": "device-key-for-test", "Device__SensorApiKey": "sensor-key-for-test"}

    def children(self, simulator=False, keys=KEYS):
        with mock.patch.object(supervisor, "env_value", lambda n: keys.get(n, "")), mock.patch.dict(os.environ, {}, clear=False):
            return supervisor.default_children(simulator)

    def test_the_simulator_is_off_unless_asked_and_keys_never_reach_a_command_line(self):
        names = [c["name"] for c in self.children()]
        self.assertEqual(names[0], "django")
        self.assertNotIn("simulator", names)
        self.assertNotIn("health", self.children()[0])  # polled every second it would flood django.log
        with_sim = self.children(simulator=True)
        sim = with_sim[-1]
        self.assertEqual(sim["name"], "simulator")
        self.assertEqual(sim["env"], self.KEYS)
        self.assertTrue(sim["restart"])  # it exits until the server answers; restarted by the policy
        for c in with_sim:
            self.assertFalse(set(self.KEYS.values()) & set(c["cmd"]), c["name"])
        with self.assertRaisesRegex(SystemExit, "The simulator needs Device__ApiKey and Device__SensorApiKey"):
            self.children(simulator=True, keys={"Device__ApiKey": "device-key-for-test"})

    def test_a_large_log_is_rotated_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "ai.log"
            log.write_bytes(b"x" * 10)
            supervisor.rotate(log)
            self.assertTrue(log.exists())  # small: kept
            with mock.patch.object(supervisor, "LOG_MAX_BYTES", 5):
                supervisor.rotate(log)
                self.assertFalse(log.exists())
                self.assertEqual((Path(tmp) / "ai.log.1").read_bytes(), b"x" * 10)
                supervisor.rotate(log)  # no log: nothing happens


@unittest.skipUnless(os.name == "nt", "start.ps1 is Windows-only")
class ScriptTests(SimpleTestCase):
    """The real start.ps1 and stop.ps1, copied into an empty folder: their own checks run, nothing else can."""

    def run_script(self, name, folder):
        shutil.copy(supervisor.ROOT / name, folder / name)
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(folder / name)],
                           capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout + r.stderr

    def test_missing_virtual_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            code, out = self.run_script("start.ps1", folder)
            self.assertEqual(code, 1)
            self.assertIn("control.py is missing", out)
            (folder / "sentinel-django" / "tools").mkdir(parents=True)
            (folder / "sentinel-django" / "tools" / "control.py").write_text("")
            code, out = self.run_script("start.ps1", folder)
            self.assertEqual(code, 1)
            self.assertIn("sentinel-django\\.venv is missing", out)
            code, out = self.run_script("stop.ps1", folder)
            self.assertEqual(code, 0)
            self.assertIn("nothing to stop", out)
