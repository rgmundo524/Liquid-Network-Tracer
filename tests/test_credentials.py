import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.cli import main


class CredentialsCheckTests(unittest.TestCase):
    def invoke(self, environment, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, environment, clear=True), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = main(["credentials-check", *arguments])
        return status, output.getvalue(), errors.getvalue()

    def test_default_checks_only_blockstream_without_exposing_values(self):
        environment = {
            "BLOCKSTREAM_CLIENT_ID": "synthetic-id\n$(not-a-command)\"'",
            "BLOCKSTREAM_CLIENT_SECRET": "synthetic-secret\x1b[31m`not-a-command`&=",
        }
        status, output, errors = self.invoke(environment)
        self.assertEqual(status, 0)
        self.assertEqual(output, "BLOCKSTREAM_CLIENT_ID: present\nBLOCKSTREAM_CLIENT_SECRET: present\n")
        self.assertEqual(errors, "")
        for value in environment.values():
            self.assertNotIn(value, output + errors)

    def test_unset_or_empty_blockstream_credential_fails(self):
        for missing in ("BLOCKSTREAM_CLIENT_ID", "BLOCKSTREAM_CLIENT_SECRET"):
            for empty in (False, True):
                with self.subTest(missing=missing, empty=empty):
                    environment = {"BLOCKSTREAM_CLIENT_ID": "synthetic-id", "BLOCKSTREAM_CLIENT_SECRET": "synthetic-secret"}
                    if empty:
                        environment[missing] = ""
                    else:
                        environment.pop(missing)
                    status, output, errors = self.invoke(environment)
                    self.assertEqual(status, 1)
                    self.assertIn(missing + ": MISSING\n", output)
                    self.assertEqual(output.count(": present\n"), 1)
                    self.assertEqual(errors, "")

    def test_miro_needs_only_access_token(self):
        status, output, errors = self.invoke({"MIRO_ACCESS_TOKEN": "synthetic-token\nsecret=42"}, "--service", "miro")
        self.assertEqual(status, 0)
        self.assertEqual(output, "MIRO_ACCESS_TOKEN: present\n")
        self.assertEqual(errors, "")
        for environment in ({}, {"MIRO_ACCESS_TOKEN": ""}, {"MIRO_CLIENT_ID": "synthetic-id", "MIRO_CLIENT_SECRET": "synthetic-secret"}):
            with self.subTest(environment=environment):
                self.assertEqual(self.invoke(environment, "--service", "miro"),
                                 (1, "MIRO_ACCESS_TOKEN: MISSING\n", ""))

    def test_all_requires_both_services(self):
        environment = {"BLOCKSTREAM_CLIENT_ID": "synthetic-id", "BLOCKSTREAM_CLIENT_SECRET": "synthetic-secret"}
        status, output, errors = self.invoke(environment, "--service", "all")
        self.assertEqual(status, 1)
        self.assertEqual(output, "BLOCKSTREAM_CLIENT_ID: present\nBLOCKSTREAM_CLIENT_SECRET: present\nMIRO_ACCESS_TOKEN: MISSING\n")
        self.assertEqual(errors, "")
        environment["MIRO_ACCESS_TOKEN"] = "synthetic-token"
        status, output, errors = self.invoke(environment, "--service", "all")
        self.assertEqual(status, 0)
        self.assertEqual(output.count(": present\n"), 3)
        self.assertEqual(errors, "")

    def test_check_never_creates_storage_fetches_secrets_or_contacts_apis(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            case = Path(directory) / "must-not-create"
            targets = ("liquid_tracer.cli.Store", "liquid_tracer.cli.Esplora", "liquid_tracer.cli.sync",
                       "liquid_tracer.cli.publish", "subprocess.run", "urllib.request.urlopen", "socket.create_connection")
            guards = [stack.enter_context(patch(target, side_effect=AssertionError("Unexpected external operation")))
                      for target in targets]
            result = self.invoke({"LIQUID_CASE_DIR": str(case)}, "--service", "all")
            self.assertEqual(result[0], 1)
            self.assertEqual(result[2], "")
            self.assertEqual(list(Path(directory).iterdir()), [])
            for guard in guards:
                guard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
