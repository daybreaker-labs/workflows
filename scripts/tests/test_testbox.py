"""Exercise the shared Testbox workflow's setup and SSH export programs offline."""

import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/testbox.yml"
FIXTURE = "scripts/tests/fixtures/testbox-setup.sh"
BASHRC = "# ~/.bashrc fixture\ncase $- in\n    *i*) ;;\n      *) return;;\nesac\nexport AFTER_GUARD=1\n"


def program(step):
    section = WORKFLOW.read_text().split(f"      - name: {step}\n", 1)[1]
    return textwrap.dedent(section.split("        run: |\n", 1)[1].split("\n      - ", 1)[0])


def step_shell(step, cwd, env):
    """Run a step program the way the runner does: bash -e with pipefail."""
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", program(step)],
        cwd=cwd, env=env, text=True, capture_output=True,
    )


def apply_github_env(text):
    """Apply GITHUB_ENV commands as the runner does before the next step."""
    values, lines = {}, iter(text.splitlines())
    for line in lines:
        heredoc = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)<<(.+)$", line)
        if heredoc:
            body = []
            for value in lines:
                if value == heredoc.group(2):
                    break
                body.append(value)
            values[heredoc.group(1)] = "\n".join(body)
        elif "=" in line:
            name, value = line.split("=", 1)
            values[name] = value
    return values


class TestboxWorkflowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.home = self.root / "home"
        self.temp = self.root / "runner-temp"
        self.home.mkdir()
        self.temp.mkdir()
        (self.home / ".bashrc").write_text(BASHRC)
        self.github_env = self.root / "github-env"
        self.github_path = self.root / "github-path"
        self.github_env.write_text("")
        self.github_path.write_text("")
        self.base = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "RUNNER_TEMP": str(self.temp),
            "GITHUB_WORKSPACE": str(ROOT),
            "GITHUB_ENV": str(self.github_env),
            "GITHUB_PATH": str(self.github_path),
        }

    def setup(self, path=FIXTURE):
        return step_shell("Run setup", ROOT, {**self.base, "SETUP": path})

    def export(self):
        """Run setup, apply its GITHUB_ENV and GITHUB_PATH, then the export step."""
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        env = {**self.base, **apply_github_env(self.github_env.read_text())}
        added = self.github_path.read_text().split()
        env["PATH"] = os.pathsep.join([*reversed(added), env["PATH"]])
        result = step_shell("Expose the environment to SSH commands", ROOT, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return env, result.stdout

    def ssh_shell(self, command):
        """A non-interactive shell that, like bash under sshd, reads ~/.bashrc first."""
        return subprocess.run(
            ["env", "-i", f"HOME={self.home}", "PATH=/usr/bin:/bin", "bash", "--norc", "-c",
             f'. "$HOME/.bashrc"\n{command}'],
            text=True, capture_output=True,
        )

    def test_setup_records_only_the_names_it_writes_to_github_env(self):
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        names = (self.temp / "testbox-ssh-env-names").read_text().split()
        self.assertEqual(names, ["TESTBOX_FIXTURE_LINES", "TESTBOX_FIXTURE_QUOTED", "TESTBOX_FIXTURE_URL"])

    def test_setup_path_must_be_an_existing_repository_relative_file(self):
        for path in ("", "/etc/passwd", "../outside.sh", "scripts/../../outside.sh", "scripts/..", "missing.sh"):
            with self.subTest(path=path):
                result = self.setup(path)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("::error::setup", result.stderr)

    def test_ssh_commands_see_setup_path_and_variables(self):
        env, stdout = self.export()
        names = ["PATH", "RUNNER_TEMP", "GITHUB_WORKSPACE", "TESTBOX_FIXTURE_LINES",
                 "TESTBOX_FIXTURE_QUOTED", "TESTBOX_FIXTURE_URL"]
        shown = self.ssh_shell('for name in ' + " ".join(names) + '; do printf "%s=%s\\0" "$name" "${!name-}"; done')
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(shown.stdout, "".join(f"{name}={env[name]}\0" for name in names))
        self.assertEqual(env["TESTBOX_FIXTURE_LINES"], "first line\nNOT_A_NAME=second line")
        tool = self.ssh_shell("testbox-fixture-tool; echo ${AFTER_GUARD:-guarded}")
        self.assertEqual(tool.stdout.split(), ["testbox-fixture-tool", "guarded"])
        self.assertNotIn("postgres://", stdout, "the log lists names, never values")

    def test_bashrc_hook_is_prepended_once(self):
        self.export()
        self.export()
        bashrc = (self.home / ".bashrc").read_text()
        self.assertEqual(bashrc.count("# testbox-ssh-env"), 1)
        self.assertTrue(bashrc.startswith('if [ -r "$HOME/.testbox-env" ]'))
        self.assertTrue(bashrc.endswith(BASHRC))

    def test_frame_order_pins_and_permissions(self):
        workflow = WORKFLOW.read_text()
        steps = re.findall(r"^      - (?:name: (.+)|uses: (\S+).*)$", workflow, re.M)
        self.assertEqual(
            [name or uses.split("@")[0] for name, uses in steps],
            ["Begin Testbox", "useblacksmith/checkout", "Run setup", "Expose the environment to SSH commands",
             "Check SSH commands see the environment", "Run Testbox"],
        )
        self.assertIn("uses: useblacksmith/begin-testbox@22ff2ea5697369864347140857d218e42815b33c # v2", workflow)
        self.assertIn("uses: useblacksmith/checkout@2b9fe87d796feea153b455ec2f3125b471a7b0e7 # v1.8.2\n"
                      "        with:\n          persist-credentials: false", workflow)
        self.assertIn("uses: useblacksmith/run-testbox@9b976c32aa3bcbffb348889673585a5deee1b2f3 # v2\n"
                      "        if: always()", workflow)
        self.assertIn("\npermissions:\n  contents: read\n", workflow)
        self.assertNotIn("secrets", workflow)
        for step in ("Run setup", "Expose the environment to SSH commands", "Check SSH commands see the environment"):
            self.assertNotIn("${{", program(step), step)


if __name__ == "__main__":
    unittest.main()
