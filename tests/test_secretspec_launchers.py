"""Run the checked-in shell launchers against a reason-enforcing provider stub."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]


def launcher(name):
    source = (PROJECT / "devenv.nix").read_text()
    body = source.split("scripts." + name + " = {", 1)[1].split("exec = ''", 1)[1].split("'';", 1)[0]
    return body.replace("''${", "${")


class SecretSpecLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = self.root / "events.jsonl"
        provider = self.root / "secretspec"
        provider.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
args = sys.argv[1:]
assert "--reason" in args, "missing access reason"
reason = args[args.index("--reason") + 1]
assert reason.strip(), "empty access reason"
with open(os.environ["FIXTURE_EVENTS"], "a") as stream:
    stream.write(json.dumps(args) + "\\n")
''')
        provider.chmod(0o755)
        self.env = {**os.environ, "LIQUID_SECRETSPEC_BIN": str(provider),
                    "LIQUID_TRACER_ROOT": str(self.root / "Project with spaces"),
                    "LIQUID_SECRET_PROVIDER": "protonpass", "LIQUID_SECRET_PROFILE": "development",
                    "FIXTURE_EVENTS": str(self.events)}
        self.env.pop("SECRETSPEC_REASON", None)

    def run_launcher(self, name, *args):
        result = subprocess.run(["bash", "-c", launcher(name), name, *args], env=self.env,
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        return [json.loads(line) for line in self.events.read_text().splitlines()]

    def test_live_launcher_supplies_reason_and_preserves_literal_cli_arguments(self):
        arguments = ["miro-sync", "--case", "/cases/private investigation", "--board", "BOARD="]
        [command] = self.run_launcher("liquid-live", *arguments)
        reason = command[command.index("--reason") + 1]
        self.assertIn("Authenticate", reason)
        self.assertNotIn("private investigation", reason)
        self.assertNotIn("BOARD=", reason)
        self.assertEqual(command[command.index("--") + 1:], ["liquid-trace", *arguments])

    def test_live_launcher_preserves_explicit_reason_without_shell_expansion(self):
        self.env["SECRETSPEC_REASON"] = "Review a case; $(false) `false` $USER"
        [command] = self.run_launcher("liquid-live", "credentials-check")
        self.assertEqual(command[command.index("--reason") + 1], self.env["SECRETSPEC_REASON"])

    def test_blank_reason_uses_a_nonblank_default(self):
        self.env["SECRETSPEC_REASON"] = " \t\n "
        [command] = self.run_launcher("liquid-live", "credentials-check")
        self.assertIn("Authenticate", command[command.index("--reason") + 1])

    def test_setup_supplies_storage_reason_for_each_credential(self):
        commands = self.run_launcher("liquid-secrets-setup", "all")
        self.assertEqual(len(commands), 3)
        self.assertEqual([args[args.index("set") + 1] for args in commands],
                         ["BLOCKSTREAM_CLIENT_ID", "BLOCKSTREAM_CLIENT_SECRET", "MIRO_ACCESS_TOKEN"])
        for args in commands:
            self.assertIn("Store API credentials", args[args.index("--reason") + 1])

    def test_setup_preserves_user_reason_and_validates_arguments_before_provider(self):
        self.env["SECRETSPEC_REASON"] = "Configure the selected investigation tool"
        [command] = self.run_launcher("liquid-secrets-setup", "miro")
        self.assertEqual(command[command.index("--reason") + 1], self.env["SECRETSPEC_REASON"])
        before = self.events.read_bytes()
        result = subprocess.run(["bash", "-c", launcher("liquid-secrets-setup"), "setup", "invalid"],
                                env=self.env, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.events.read_bytes(), before)
