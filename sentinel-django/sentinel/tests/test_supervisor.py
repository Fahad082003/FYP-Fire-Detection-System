"""tools/supervisor.py with real Windows processes: fake children only, each supervisor in its own hidden console, temp folders.
Nothing here starts Django, the AI service or a database, and no process the test did not start is ever stopped."""
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

TOOLS = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))
import supervisor  # noqa: E402

# argv: children JSON, run dir, log dir, stop seconds, bystander pid file ("" = none), tools dir
LAUNCHER = """
import json, os, subprocess, sys
sys.path.insert(0, sys.argv[6])
import supervisor
if sys.argv[5]:  # an unrelated process in the supervisor's own console, started before its job: nothing may stop it
    b = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(40)"])
    open(sys.argv[5], "w").write(json.dumps({"pid": b.pid, "start": supervisor.process_start(b.pid)}))
if os.environ.get("TEST_JOB_FAILS"):  # what Windows does when a job cannot be made or joined
    def refuse():
        raise OSError(5, "Access is denied")
    supervisor.join_kill_on_close_job = refuse
supervisor.STOP_SECONDS = float(sys.argv[4])
supervisor.setup_logging(sys.argv[3])
sys.exit(supervisor.Supervisor(json.loads(sys.argv[1]), sys.argv[2], sys.argv[3]).run())
"""
# argv: seconds to live, exit code, "ignore" = ignore Ctrl+Break
CHILD = """
import os, signal, sys, time
def on_break(*_):
    print("child got ctrl+break", flush=True)
    sys.exit(0)
signal.signal(signal.SIGBREAK, signal.SIG_IGN if sys.argv[3:] == ["ignore"] else on_break)
print("child up", os.getpid(), flush=True)
end = time.monotonic() + float(sys.argv[1])
while time.monotonic() < end:
    time.sleep(0.05)
sys.exit(int(sys.argv[2]))
"""
# A stand-in for the AI service. argv: port, seconds of "loading", status after that
HEALTH_CHILD = """
import json, os, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
port, loading, final = int(sys.argv[1]), float(sys.argv[2]), sys.argv[3]
t0 = time.monotonic()
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"status": final if time.monotonic() - t0 >= loading else "loading"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
print("health child up", os.getpid(), flush=True)
HTTPServer(("127.0.0.1", port), Health).serve_forever()
"""
FAST = {"max_restarts": 2, "window": 60, "initial": 0.2, "max_backoff": 0.4}


def child(name, seconds=120, code=0, ignore_break=False, port=None, **extra):
    spec = {"name": name, "cwd": str(TOOLS), "cmd": [sys.executable, "-c", CHILD, str(seconds), str(code)] + (["ignore"] if ignore_break else [])}
    return {**spec, **({"port": port} if port else {}), **extra}


def health_child(name, port, loading=1.0, final="ok", restart=FAST):
    return {"name": name, "cwd": str(TOOLS), "port": port, "health": f"http://127.0.0.1:{port}/health", "restart": restart,
            "cmd": [sys.executable, "-c", HEALTH_CHILD, str(port), str(loading), final]}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(os.name == "nt", "the supervisor is Windows-only")
class SupervisorTests(SimpleTestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="sentinel-supervisor-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.run_dir, self.log_dir = self.dir / "run", self.dir / "logs"
        self.state_path = self.run_dir / "supervisor.json"

    def supervise(self, children, stop_seconds=10, bystander="", env=None):
        hidden = subprocess.STARTUPINFO(dwFlags=subprocess.STARTF_USESHOWWINDOW, wShowWindow=0)
        p = subprocess.Popen([sys.executable, "-c", LAUNCHER, json.dumps(children), str(self.run_dir), str(self.log_dir),
                              str(stop_seconds), bystander, str(TOOLS)], creationflags=subprocess.CREATE_NEW_CONSOLE,
                             startupinfo=hidden, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             env={**os.environ, **(env or {})})
        self.addCleanup(self.cleanup, p)
        return p

    def cleanup(self, p):
        if p.poll() is None:  # a failed test: stop it the normal way, so its children go too
            self.run_dir.mkdir(parents=True, exist_ok=True)
            (self.run_dir / "stop").touch()
            p.wait(30)
        p.stdout.close()

    def wait_for(self, condition, seconds=20):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            value = condition()
            if value:
                return value
            time.sleep(0.05)
        self.fail("condition not met in time")

    def child_state(self, name, *states):
        """The live supervisor's state once child `name` is in one of `states`."""
        def ready():
            state = supervisor.read_state(self.state_path)
            fresh = supervisor.same_process(state.get("supervisor", {}))  # written by the live supervisor, not a file left before
            return state if fresh and state.get("children", {}).get(name, {}).get("state") in states else None
        return self.wait_for(ready)

    def running_state(self, *names):
        for name in names:
            state = self.child_state(name, "running")
        return state

    def stop(self, p):
        (self.run_dir / "stop").touch()
        code = p.wait(30)
        if code != 0:  # read only on failure: a bystander inherits the pipe and keeps it open
            self.fail(f"supervisor exited with {code}: {p.stdout.read()}")

    def log(self, name):
        return (self.log_dir / f"{name}.log").read_text(encoding="utf-8")

    def real_pid(self, name, nth=1):
        """The interpreter behind the venv launcher (.venv\\Scripts\\python.exe starts it as its own child)."""
        found = self.wait_for(lambda: len(m := re.findall(r"child up (\d+)", self.log(name))) >= nth and m)
        return int(found[nth - 1])

    def test_starts_children_records_their_identity_logs_them_and_cleans_up(self):
        port = free_port()
        bystander = self.dir / "bystander.json"
        p = self.supervise([child("a", env={"SENTINEL_DEVICE_KEY": "secret-value-4711"}), child("b", port=port)],
                           bystander=str(bystander))
        state = self.running_state("a", "b")
        # p is the venv launcher (.venv\Scripts\python.exe starts the real interpreter as its child): its PID differs.
        self.assertTrue(supervisor.same_process(state["supervisor"]))
        self.assertNotIn(state["supervisor"]["pid"], (p.pid, os.getpid()))
        pids = {}
        for name in ("a", "b"):
            entry = state["children"][name]
            self.assertTrue(supervisor.same_process(entry), name)  # PID + creation time match the running child
            pids[name] = entry["pid"]
        self.assertEqual(state["children"]["b"]["port"], port)
        self.wait_for(lambda: "child up" in self.log("a") and "child up" in self.log("b"))
        self.assertIn("started by supervisor PID", self.log("a"))
        self.assertFalse(list(self.run_dir.glob("*.tmp")))  # atomic writes leave no temp file behind
        self.assertNotIn("secret-value-4711", self.state_path.read_text() + self.log("supervisor"))  # a key never leaks

        self.stop(p)
        for name, pid in pids.items():
            self.assertIsNone(supervisor.process_start(pid), name)
            self.assertIn("child got ctrl+break", self.log(name))  # stopped gracefully, not terminated
        self.assertFalse(self.state_path.exists())
        self.assertFalse((self.run_dir / "stop").exists())
        self.assertRegex(self.log("supervisor"), r"a stopped \(code 0\)[\s\S]*supervisor stopped")
        # Ctrl+Break went only to the children's own process groups, not to the console they share with this process.
        bystander = json.loads(bystander.read_text())
        self.assertTrue(supervisor.same_process(bystander))
        os.kill(bystander["pid"], signal.SIGTERM)  # this test's own process, identity checked just above

    def test_start_and_stop_can_be_repeated(self):
        for round_ in range(3):
            with self.subTest(round=round_):
                p = self.supervise([child("a"), child("b")])
                state = self.running_state("a", "b")
                pids = [state["children"][n]["pid"] for n in ("a", "b")] + [self.real_pid("a", round_ + 1)]
                self.stop(p)
                self.assertEqual([supervisor.process_start(pid) for pid in pids], [None, None, None])
                self.assertFalse(self.state_path.exists())

    def test_a_second_supervisor_is_refused_and_the_first_is_untouched(self):
        first = self.supervise([child("a")])
        state = self.running_state("a")
        second = self.supervise([child("a")])
        self.assertEqual(second.wait(30), 2)
        self.assertIn(f"a supervisor is already running (PID {state['supervisor']['pid']})", second.stdout.read())
        self.assertTrue(supervisor.same_process(state["children"]["a"]))
        self.assertEqual(supervisor.read_state(self.state_path)["children"]["a"]["pid"], state["children"]["a"]["pid"])
        self.stop(first)

    def test_a_busy_port_is_refused_before_any_child_starts(self):
        for reuse in (False, True):  # an ordinary server, and one sharing the port through SO_REUSEADDR (the old AI service)
            with self.subTest(reuse=reuse), socket.socket() as server:
                if reuse:
                    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind(("127.0.0.1", 0))
                server.listen()
                port = server.getsockname()[1]
                p = self.supervise([child("a", port=port)])
                self.assertEqual(p.wait(30), 2)
                self.assertIn(f"port in use: a port {port}", p.stdout.read())
                self.assertFalse((self.log_dir / "a.log").exists())  # nothing was started
                with socket.create_connection(("127.0.0.1", port), timeout=2):
                    pass  # the server that held the port still answers

    def test_without_a_job_object_nothing_starts(self):
        p = self.supervise([child("a")], env={"TEST_JOB_FAILS": "1"})
        self.assertEqual(p.wait(30), 2)
        self.assertIn("cannot create the Windows job object", p.stdout.read())
        self.assertFalse((self.log_dir / "a.log").exists())

    def test_a_stale_or_corrupt_state_file_is_not_trusted(self):
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone_entry = {"pid": gone.pid, "start": supervisor.process_start(gone.pid) or 1}
        gone.wait()
        stale = {"supervisor": gone_entry, "heartbeat": "2026-01-01T00:00:00+00:00", "children": {"a": {**gone_entry, "state": "running"}}}
        for content in (json.dumps(stale), "{not json"):
            with self.subTest(content=content[:20]):
                self.run_dir.mkdir(parents=True, exist_ok=True)
                self.state_path.write_text(content)
                p = self.supervise([child("a")])
                state = self.running_state("a")
                self.assertTrue(supervisor.same_process(state["children"]["a"]))  # Windows may hand the dead PID to it
                self.stop(p)

    def test_a_reused_pid_is_never_stopped_and_a_live_earlier_child_blocks_the_start(self):
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)  # this test's own process, through its own handle
        real = {"pid": other.pid, "start": supervisor.process_start(other.pid)}
        self.run_dir.mkdir(parents=True, exist_ok=True)

        # Same PID, different creation time: the PID was reused by an unrelated process. Ignored, never stopped.
        self.state_path.write_text(json.dumps({"children": {"a": {**real, "start": real["start"] + 1}}}))
        p = self.supervise([child("a")])
        self.assertNotEqual(self.running_state("a")["children"]["a"]["pid"], other.pid)
        self.stop(p)
        self.assertTrue(supervisor.same_process(real))

        # The very process recorded is alive (e.g. left by an older supervisor): reported and left running, nothing started.
        self.state_path.write_text(json.dumps({"children": {"a": real}}))
        p = self.supervise([child("a")])
        self.assertEqual(p.wait(30), 2)
        self.assertIn(f"a from an earlier run is still running (PID {other.pid}); it was not touched", p.stdout.read())
        self.assertTrue(supervisor.same_process(real))
        self.assertTrue(self.state_path.exists())  # the evidence is kept

    def test_a_killed_supervisor_takes_its_children_with_it_and_nothing_else(self):
        bystander = self.dir / "bystander.json"
        p = self.supervise([child("a", ignore_break=True), health_child("ai", free_port(), loading=0)], bystander=str(bystander))
        state = self.child_state("ai", "ready")
        self.running_state("a")
        launchers = [state["children"][n] for n in ("a", "ai")]
        real = self.real_pid("a")
        self.assertTrue(supervisor.same_process(state["supervisor"]))
        os.kill(state["supervisor"]["pid"], signal.SIGTERM)  # TerminateProcess: a crash, no cleanup code runs
        p.wait(30)
        # Windows ends the job: the venv launchers and the interpreter behind one, though "a" even ignores Ctrl+Break.
        self.wait_for(lambda: not any(map(supervisor.same_process, launchers)) and supervisor.process_start(real) is None, 10)
        bystander = json.loads(bystander.read_text())
        self.assertTrue(supervisor.same_process(bystander))  # started outside the job: untouched
        os.kill(bystander["pid"], signal.SIGTERM)  # this test's own process, identity checked just above

        again = self.supervise([child("a")])  # the dead children's state file is stale now: a normal start
        self.running_state("a")
        self.stop(again)

    def test_a_child_without_a_restart_policy_that_exits_is_detected_and_logged(self):
        p = self.supervise([child("a", seconds=0.5, code=7), child("b")])
        state = self.child_state("a", "exited")
        self.assertEqual(state["children"]["a"]["exitCode"], 7)
        self.assertEqual(state["children"]["a"]["restarts"], 0)
        self.assertEqual(state["children"]["b"]["state"], "running")  # one child exiting leaves the others alone
        self.assertIn("a exited with code 7", self.log("supervisor"))
        self.stop(p)

    def test_a_crashing_child_is_restarted_with_growing_waits_until_the_limit(self):
        p = self.supervise([child("ai", seconds=0.3, code=3, restart=FAST), child("b")])
        state = self.child_state("ai", "failed")
        entry = state["children"]["ai"]
        self.assertEqual((entry["restarts"], entry["exitCode"]), (2, 3))
        self.assertIsNotNone(entry["lastRestartAt"])
        self.assertIsNone(entry["lastReadyAt"])
        log = self.log("supervisor")
        self.assertRegex(log, r"ai will restart in 0\.2 s[\s\S]*restarting ai \(restart 1\)[\s\S]*ai will restart in 0\.4 s"
                              r"[\s\S]*restarting ai \(restart 2\)[\s\S]*ai: not restarted again, it already restarted 2 times within 60 s")
        self.assertEqual(len(re.findall(r"child up \d+", self.log("ai"))), 3)  # first start + 2 restarts
        time.sleep(1)
        self.assertEqual(supervisor.read_state(self.state_path)["children"]["ai"]["restarts"], 2)  # no loop after giving up
        self.assertEqual(state["children"]["b"]["state"], "running")
        self.stop(p)

    def test_ready_only_when_health_says_ok_and_a_crash_is_recovered(self):
        port = free_port()
        p = self.supervise([health_child("ai", port, loading=1.5)])
        loading = self.child_state("ai", "starting")
        self.wait_for(lambda: supervisor.read_state(self.state_path)["children"]["ai"]["health"] == "loading")
        self.assertIsNone(supervisor.read_state(self.state_path)["children"]["ai"]["lastReadyAt"])  # running is not ready
        ready = self.child_state("ai", "ready")["children"]["ai"]
        self.assertIsNotNone(ready["lastReadyAt"])

        first = ready["pid"]
        self.assertTrue(supervisor.same_process(ready))
        os.kill(first, signal.SIGTERM)  # the AI service crashes (a child this test's supervisor started, identity checked)
        recovered = self.wait_for(lambda: (c := supervisor.read_state(self.state_path)["children"]["ai"])["restarts"] == 1
                                  and c["state"] == "ready" and c)
        self.assertNotEqual(recovered["pid"], first)
        self.assertEqual(recovered["exitCode"], 15)  # os.kill on Windows: TerminateProcess with the signal number as exit code
        self.assertGreater(recovered["lastReadyAt"], ready["lastReadyAt"])
        self.assertRegex(self.log("supervisor"), r"ai exited with code 15[\s\S]*ai will restart in 0\.2 s[\s\S]*restarting ai"
                                                 r"[\s\S]*ai is not ready \(/health: [^)]*\)[\s\S]*ai is ready")
        self.assertEqual(loading["children"]["ai"]["state"], "starting")
        self.stop(p)

    def test_a_service_whose_models_failed_never_counts_as_ready(self):
        p = self.supervise([health_child("ai", free_port(), loading=0, final="no-model")])
        self.wait_for(lambda: supervisor.read_state(self.state_path).get("children", {}).get("ai", {}).get("health") == "no-model")
        time.sleep(1.5)
        entry = supervisor.read_state(self.state_path)["children"]["ai"]
        self.assertEqual((entry["state"], entry["lastReadyAt"], entry["restarts"]), ("starting", None, 0))
        self.stop(p)

    def test_a_child_that_ignores_ctrl_break_is_terminated_after_the_wait(self):
        p = self.supervise([child("a", ignore_break=True)], stop_seconds=1)
        pid = self.running_state("a")["children"]["a"]["pid"]
        real = self.real_pid("a")
        self.stop(p)
        self.assertIsNone(supervisor.process_start(pid))
        self.assertIsNone(supervisor.process_start(real))  # terminating the launcher also ends the real child, no orphan
        self.assertIn("a did not stop within 1.0 s: terminated", self.log("supervisor"))

    def test_a_failed_start_stops_the_children_already_started(self):
        p = self.supervise([child("a"), {"name": "b", "cwd": str(TOOLS), "cmd": [str(self.dir / "missing.exe")]}])
        self.assertEqual(p.wait(30), 1)
        log = self.log("supervisor")
        self.assertIn("start failed", log)
        pid = int(re.search(r"started a \(PID (\d+)\)", log).group(1))
        self.assertIsNone(supervisor.process_start(pid))
        self.assertFalse(self.state_path.exists())


class RestartPolicyTests(SimpleTestCase):
    P = {"max_restarts": 5, "window": 600, "initial": 2, "max_backoff": 60}

    def test_the_wait_doubles_up_to_the_maximum(self):
        self.assertEqual([supervisor.restart_delay([], n, 0, self.P) for n in range(8)], [2, 4, 8, 16, 32, 60, 60, 60])

    def test_the_limit_counts_only_restarts_inside_the_window(self):
        five = [0, 10, 20, 30, 40]
        self.assertIsNone(supervisor.restart_delay(five, 0, 50, self.P))  # 5 restarts in the last 10 minutes: stop
        self.assertEqual(supervisor.restart_delay(five, 0, 600, self.P), 2)  # the first one left the window: allowed again
        self.assertIsNone(supervisor.restart_delay([], 0, 0, {**self.P, "max_restarts": 0}))  # 0 = never restart

    def test_after_being_ready_the_next_crash_waits_the_shortest_time_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = supervisor.Supervisor([{"name": "ai", "cmd": [], "cwd": tmp, "health": "http://127.0.0.1:9/health", "restart": self.P}],
                                      tmp, tmp)
            s.state["children"]["ai"] = {"state": "starting", "health": None, "log": "ai.log", "lastReadyAt": None}
            s.failures["ai"] = 4  # it had crashed four times in a row
            with mock.patch.object(supervisor, "health_status", return_value="ok"):
                s.check_ready("ai")
            self.assertEqual(s.state["children"]["ai"]["state"], "ready")
            s.on_exit("ai", 1)
            self.assertEqual(s.state["children"]["ai"]["backoffSeconds"], 2)

    def test_defaults_and_overrides_from_the_environment(self):
        self.assertEqual(supervisor.restart_policy({}), self.P)
        self.assertEqual(supervisor.restart_policy({"Supervisor__MaxRestarts": "3", "Supervisor__RestartWindowSeconds": "120",
                                                    "Supervisor__InitialBackoffSeconds": "0.5", "Supervisor__MaxBackoffSeconds": "10"}),
                         {"max_restarts": 3, "window": 120.0, "initial": 0.5, "max_backoff": 10.0})

    def test_an_invalid_value_stops_the_start_with_its_name(self):
        for env, message in (({"Supervisor__MaxRestarts": "five"}, "Supervisor__MaxRestarts='five' is not a number"),
                             ({"Supervisor__MaxRestarts": "2.5"}, "must be a whole number"),
                             ({"Supervisor__MaxRestarts": "-1"}, "must be 0 or more"),
                             ({"Supervisor__RestartWindowSeconds": "0"}, "Supervisor__RestartWindowSeconds='0' must be more than 0"),
                             ({"Supervisor__MaxBackoffSeconds": "inf"}, "Supervisor__MaxBackoffSeconds='inf' must be more than 0"),
                             ({"Supervisor__InitialBackoffSeconds": "90"}, "must not be more than Supervisor__MaxBackoffSeconds")):
            with self.subTest(env=env), self.assertRaisesRegex(SystemExit, re.escape(message)):
                supervisor.restart_policy(env)


@unittest.skipUnless(os.name == "nt", "the supervisor is Windows-only")
class ProcessIdentityTests(SimpleTestCase):
    def test_creation_time_identifies_a_running_process_only(self):
        me = supervisor.process_start(os.getpid())
        self.assertIsNotNone(me)
        self.assertEqual(supervisor.process_start(os.getpid()), me)
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()  # our handle keeps the exited process object alive: it must still count as not running
        self.assertIsNone(supervisor.process_start(p.pid))
        self.assertFalse(supervisor.same_process({"pid": os.getpid(), "start": me + 1}))
        self.assertFalse(supervisor.same_process({"pid": "1", "start": me}))

    def test_port_free_sees_every_kind_of_holder(self):
        for host, reuse, listen in (("127.0.0.1", False, True), ("127.0.0.1", True, True), ("0.0.0.0", False, True),
                                    ("127.0.0.1", False, False)):  # 0.0.0.0: Windows still lets 127.0.0.1 be bound
            with self.subTest(host=host, reuse=reuse, listen=listen), socket.socket() as s:
                if reuse:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, 0))
                if listen:
                    s.listen()
                self.assertFalse(supervisor.port_free(s.getsockname()[1]))
        self.assertTrue(supervisor.port_free(free_port()))
