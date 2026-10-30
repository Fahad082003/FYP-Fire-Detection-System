"""e2e/run.py refuses to start while a test port is taken: a server left from an interrupted run would answer instead."""
import importlib.util
import socket
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

_spec = importlib.util.spec_from_file_location("e2e_run", Path(__file__).resolve().parents[2] / "e2e" / "run.py")
run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run)  # module level only defines constants and functions


class E2eRunnerTests(SimpleTestCase):
    def test_a_port_in_use_is_refused_before_the_test_database_is_touched(self):
        with socket.socket() as leftover:
            leftover.bind(("127.0.0.1", 0))
            leftover.listen()
            port = leftover.getsockname()[1]
            with self.assertRaisesRegex(SystemExit, str(port)):
                run.require_free_ports(port)
            with mock.patch.object(run, "PORT", port), mock.patch.object(run, "create_db") as create_db, \
                    mock.patch.object(run.subprocess, "Popen") as popen, self.assertRaises(SystemExit):
                run.main()
            create_db.assert_not_called()
            popen.assert_not_called()
        run.require_free_ports(port)  # free again: no error
