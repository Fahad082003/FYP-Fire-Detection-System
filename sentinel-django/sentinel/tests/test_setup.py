"""tools/setup_check.py and setup.ps1 (first-time setup on a new PC). Everything runs in temp folders or reads only: no .env of
the project, no real database (an unreachable server), no download, no pip install (every question is answered "no")."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

TOOLS = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))
import setup_check  # noqa: E402
import supervisor  # noqa: E402

NO = lambda question: False  # noqa: E731 - a person who answers "no"
YES = lambda question: True  # noqa: E731


@unittest.skipUnless(os.name == "nt", "the setup is Windows-only")
class SetupCheckTests(SimpleTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="sentinel-setup-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_only_python_3_13_is_accepted(self):
        self.assertIsNone(setup_check.check_python((3, 13, 1)))
        message = setup_check.check_python((3, 12, 4))
        self.assertIn("This is Python 3.12; Sentinel needs Python 3.13", message)
        self.assertIn("https://www.python.org/downloads/windows/", message)

    def test_a_missing_venv_is_created_only_after_a_yes_and_missing_packages_are_never_installed_without_one(self):
        self.assertIn("ai-service\\.venv is missing. Create it: py -3.13 -m venv ai-service\\.venv",
                      setup_check.check_venv(self.root, "ai-service", ("json",), NO))
        self.assertFalse((self.root / "ai-service" / ".venv").exists())
        answers = []
        message = setup_check.check_venv(self.root, "ai-service", ("json", "no_such_package_xyz"),
                                         lambda q: answers.append(q) or not q.startswith("ai-service: packages"))
        self.assertTrue((self.root / "ai-service" / ".venv" / "Scripts" / "python.exe").is_file())  # made, after the yes
        self.assertIn("ai-service: packages missing (no_such_package_xyz). Install them: ai-service\\.venv\\Scripts\\python -m pip "
                      "install -r ai-service\\requirements.txt", message)
        self.assertIn("downloads from the internet", answers[-1])  # asked first, answered no: pip never ran
        self.assertIsNone(setup_check.check_venv(self.root, "ai-service", ("json",), NO))

    def test_a_venv_whose_python_no_longer_runs_is_explained(self):
        scripts = self.root / "sentinel-django" / ".venv" / "Scripts"
        scripts.mkdir(parents=True)
        shutil.copy(Path(sys.executable), scripts / "python.exe")  # the venv launcher without its pyvenv.cfg: it cannot start
        self.assertIn("was made with a Python that no longer runs, not 3.13. Delete the sentinel-django\\.venv folder",
                      setup_check.check_venv(self.root, "sentinel-django", ("json",), NO))

    def test_a_missing_env_is_copied_from_the_example_only_after_a_yes_and_never_overwritten(self):
        with mock.patch.dict(os.environ, {"SENTINEL_NO_DOTENV": ""}):
            values, problem = setup_check.env_values(self.root, YES)  # no .env.example either: nothing to copy
            self.assertIn("No .env file in the project folder", problem)
            (self.root / ".env.example").write_text("Jwt__SigningKey=\nDevice__ApiKey=\n")
            self.assertIsNotNone(setup_check.env_values(self.root, NO)[1])
            self.assertFalse((self.root / ".env").exists())
            values, problem = setup_check.env_values(self.root, YES)
            self.assertIsNone(problem)
            self.assertEqual((self.root / ".env").read_text(), "Jwt__SigningKey=\nDevice__ApiKey=\n")
            (self.root / ".env").write_text("Jwt__SigningKey=mine\n")
            setup_check.env_values(self.root, YES)
            self.assertEqual((self.root / ".env").read_text(), "Jwt__SigningKey=mine\n")  # an existing .env is never replaced

    def test_required_settings_are_named_and_their_values_never_shown(self):
        self.assertEqual(len(setup_check.check_env({})), 2)  # signing key and AI key; the sensor key may stay empty
        problems = setup_check.check_env({"Jwt__SigningKey": "short-Secret-1", "Device__ApiKey": "same-Key-77",
                                          "Device__SensorApiKey": "same-Key-77"})
        text = "\n".join(problems)
        self.assertIn("Jwt__SigningKey must be at least 32 characters", text)
        self.assertIn("Device__SensorApiKey must differ from Device__ApiKey", text)
        self.assertNotIn("short-Secret-1", text)
        self.assertNotIn("same-Key-77", text)
        self.assertEqual(setup_check.check_env({"Jwt__SigningKey": "x" * 32, "Device__ApiKey": "a", "Device__SensorApiKey": ""}), [])

    def test_an_unavailable_database_is_explained_without_the_password(self):
        env = {"SENTINEL_NO_DOTENV": "1", "Jwt__SigningKey": "test-only-signing-key-0123456789abcdef",
               "ConnectionStrings__Sentinel": "Server=127.0.0.1,1;Database=test_unreachable;User Id=u;Password=Pw-never-shown-42;"
                                              "TrustServerCertificate=True"}
        with mock.patch.dict(os.environ, env):
            message = setup_check.check_database(supervisor.ROOT, ["Pw-never-shown-42"])
        self.assertIn("Database unavailable (cannot connect (OperationalError))", message)
        self.assertIn("services.msc", message)
        self.assertIn(".\\database\\load.ps1 (it OVERWRITES a local SentinelDb", message)
        self.assertNotIn("Pw-never-shown-42", message)

    def test_a_database_without_the_sentinel_tables_points_to_the_restore(self):
        empty = subprocess.CompletedProcess([], 0, stdout="empty\n", stderr="")
        with mock.patch.object(setup_check.subprocess, "run", return_value=empty):
            self.assertIn("SentinelDb has no Sentinel tables", setup_check.check_database(self.root))
        with mock.patch.object(setup_check.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="tables\n")):
            self.assertIsNone(setup_check.check_database(self.root))

    def test_a_missing_odbc_driver_is_named_with_its_download(self):
        with mock.patch.dict(os.environ, {"SENTINEL_ODBC_DRIVER": "No Such Driver 99"}):
            self.assertIn("'No Such Driver 99' is not installed", setup_check.check_odbc(supervisor.ROOT))

    def test_model_files_are_checked_against_the_manifest_and_never_downloaded(self):
        models = self.root / "ai-service" / "models"
        models.mkdir(parents=True)
        good = b"weights"
        manifest = {"models": [
            {"name": "person", "file": "p.onnx", "url": "https://github.com/x/p.onnx", "sha256": hashlib.sha256(good).hexdigest()},
            {"name": "fire_smoke", "file": "f.onnx", "url": None, "sha256": hashlib.sha256(good).hexdigest()}]}
        (models / "models.json").write_text(json.dumps(manifest))
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("download")):
            missing = setup_check.check_models(self.root)
            self.assertIn("Model person: p.onnx is missing; download it (from github.com, sha256-checked): "
                          "ai-service\\.venv\\Scripts\\python ai-service\\tools\\download_models.py", missing[0])
            self.assertIn("Model fire_smoke: f.onnx is missing; it is not in git and has no download: copy "
                          "ai-service\\models\\f.onnx from the other PC", missing[1])
            (models / "p.onnx").write_bytes(good)
            (models / "f.onnx").write_bytes(b"damaged")
            [wrong] = setup_check.check_models(self.root)
            self.assertIn("f.onnx is not the expected file (sha256 mismatch", wrong)
            (models / "f.onnx").write_bytes(good)
            self.assertEqual(setup_check.check_models(self.root), [])
        self.assertEqual(sorted(p.name for p in models.iterdir()), ["f.onnx", "models.json", "p.onnx"])  # nothing added

    def test_the_installed_models_match_their_manifest(self):
        self.assertEqual(setup_check.check_models(supervisor.ROOT), [])  # reads the real files only

    def test_with_no_console_every_question_is_a_no(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=False), mock.patch("builtins.input") as typed:
            self.assertFalse(setup_check.ask("Install?"))
        typed.assert_not_called()


@unittest.skipUnless(os.name == "nt", "the setup is Windows-only")
class SetupScriptTests(SimpleTestCase):
    """The real setup.ps1, copied into an empty folder."""

    def run_setup(self, folder, path):
        shutil.copy(supervisor.ROOT / "setup.ps1", folder / "setup.ps1")
        powershell = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        r = subprocess.run([str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(folder / "setup.ps1")],
                           capture_output=True, text=True, timeout=60, env={**os.environ, "PATH": path})
        return r.returncode, r.stdout + r.stderr

    def test_missing_checker_and_missing_python_are_explained(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            code, out = self.run_setup(folder, os.environ["PATH"])
            self.assertEqual((code, "setup_check.py is missing" in out), (1, True), out)
            (folder / "sentinel-django" / "tools").mkdir(parents=True)
            (folder / "sentinel-django" / "tools" / "setup_check.py").write_text("")
            system32 = str(Path(os.environ["SystemRoot"]) / "System32")  # no py launcher, no python: a PC without Python
            code, out = self.run_setup(folder, system32)
            self.assertEqual(code, 1, out)
            self.assertIn("Python 3.13 was not found on this PC", out)
            self.assertIn("https://www.python.org/downloads/windows/", out)

    def test_the_checker_runs_with_python_3_13_and_its_exit_code_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "sentinel-django" / "tools").mkdir(parents=True)
            (folder / "sentinel-django" / "tools" / "setup_check.py").write_text(
                "import sys; print('checker', '%d.%d' % sys.version_info[:2], sys.argv[1:]); sys.exit(7)")
            code, out = self.run_setup(folder, os.environ["PATH"])
            self.assertEqual(code, 7, out)
            self.assertIn("checker 3.13 []", out)  # the script itself, no stray arguments
