"""Check native linter selection for the routed CI runner architectures."""

import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


class NativeLinters(unittest.TestCase):
    def test_installer_selects_matching_archives_and_rejects_unknown_architectures(self):
        workflow = Path(__file__).resolve().parents[2] / ".github/workflows/checks.yml"
        section = workflow.read_text().split("      - name: Install pinned workflow linters\n", 1)[1]
        script = textwrap.dedent(section.split("        run: |\n", 1)[1].split("\n      - ", 1)[0])
        for machine, actionlint, zizmor in (
            ("x86_64", "amd64", "x86_64"), ("aarch64", "arm64", "aarch64"), ("unknown", None, None)
        ):
            with self.subTest(machine=machine), tempfile.TemporaryDirectory() as directory:
                result = subprocess.run(
                    ["bash", "-euo", "pipefail", "-c", """
                    uname() { echo "$TEST_MACHINE"; }
                    curl() { printf '%s\\n' "$3"; }
                    sha256sum() { cat > /dev/null; }
                    tar() { :; }
                    """ + script],
                    env={**os.environ, "TEST_MACHINE": machine, "RUNNER_TEMP": directory,
                         "GITHUB_PATH": str(Path(directory) / "path")},
                    text=True, capture_output=True,
                )
                if actionlint is None:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("Unsupported runner architecture", result.stdout)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(f"actionlint_1.7.12_linux_{actionlint}.tar.gz", result.stdout)
                    self.assertIn(f"zizmor-{zizmor}-unknown-linux-gnu.tar.gz", result.stdout)
