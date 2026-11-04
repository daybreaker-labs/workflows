"""Exercise the reusable workflow's release gates and promotion program offline."""

import base64
import io
import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/product-image.yml"


def program(step):
    section = WORKFLOW.read_text().split(f"      - name: {step}\n", 1)[1]
    return textwrap.dedent(section.split("        run: |\n", 1)[1].split("\n      - ", 1)[0])


def run(command, cwd, **env):
    return subprocess.run(
        command, cwd=cwd, env={**os.environ, **env}, text=True, capture_output=True, check=True
    ).stdout.strip()


class SourceTests(unittest.TestCase):
    def verify(self, ref="refs/heads/develop", event="workflow_dispatch", publish="true", promote="false", **env):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run(["git", "init", "-b", "main"], root)
            run(
                [
                    "git",
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "--allow-empty",
                    "-m",
                    "fixture",
                ],
                root,
            )
            run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], root)
            if env.pop("unmerged", False):
                run(["git", "checkout", "--orphan", "unmerged"], root)
                run(
                    [
                        "git",
                        "-c",
                        "commit.gpgsign=false",
                        "-c",
                        "core.hooksPath=/dev/null",
                        "-c",
                        "user.name=Test",
                        "-c",
                        "user.email=test@example.com",
                        "commit",
                        "--allow-empty",
                        "-m",
                        "unmerged",
                    ],
                    root,
                )
            output = root / "output"
            run(
                [
                    "bash",
                    "-euo",
                    "pipefail",
                    "-c",
                    "uname() { echo aarch64; }\n" + program("Validate source and release ancestry"),
                ],
                root,
                **{
                    "IMAGE_NAME": "explicavet-api",
                    "GITHUB_REPOSITORY": "daybreaker-labs/explicavet-api",
                    "GITHUB_REF": ref,
                    "GITHUB_EVENT_NAME": event,
                    "PUBLISH": publish,
                    "PROMOTE": promote,
                    "GITHUB_OUTPUT": str(output),
                    **env,
                },
            )
            return dict(line.split("=", 1) for line in output.read_text().splitlines())

    def test_develop_qualification_uses_full_sha(self):
        result = self.verify()
        self.assertEqual(result["tag"], "sha-" + result["sha"])

    def test_pr_can_build_but_cannot_publish(self):
        self.verify(ref="refs/pull/1/merge", event="pull_request", publish="false")
        with self.assertRaises(subprocess.CalledProcessError):
            self.verify(ref="refs/pull/1/merge", event="pull_request")

    def test_stable_release_requires_main_and_can_promote(self):
        self.assertEqual(self.verify(ref="refs/tags/v1.2.3", event="push", promote="true")["tag"], "v1.2.3")
        with self.assertRaises(subprocess.CalledProcessError):
            self.verify(ref="refs/tags/v1.2.3", event="push", unmerged=True)

    def test_sha_cannot_promote_and_foreign_repo_cannot_build(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.verify(promote="true")
        with self.assertRaises(subprocess.CalledProcessError):
            self.verify(GITHUB_REPOSITORY="another-org/explicavet-api")

    def test_tampered_archive_fails_before_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "product-image").mkdir()
            (root / "product-image/image.tar.gz").write_bytes(b"tampered")
            with self.assertRaises(subprocess.CalledProcessError):
                run(
                    [
                        "bash",
                        "-euo",
                        "pipefail",
                        "-c",
                        "docker() { touch docker-called; }\n" + program("Verify archive before credentials"),
                    ],
                    root,
                    ARCHIVE_SHA256="a" * 64,
                )
            self.assertFalse((root / "docker-called").exists())


class BuildTests(unittest.TestCase):
    def test_go_version_and_source_reach_the_builder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Dockerfile").write_text("FROM alpine@sha256:" + "a" * 64 + "\n")
            (root / "go.mod").write_text("module example.com/api\n\ngo 1.27.3\ntoolchain go1.27.4\n")
            self.build(root)
            arguments = (root / "build-args").read_text().splitlines()
            self.assertIn("GO_VERSION=1.27.3", arguments)
            self.assertNotIn("GO_VERSION=1.27.4", arguments)
            self.assertIn("VCS_REF=" + "b" * 40, arguments)

    def test_non_go_image_needs_no_go_argument(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Dockerfile").write_text("FROM alpine@sha256:" + "a" * 64 + "\n")
            self.build(root)
            self.assertNotIn("GO_VERSION=", (root / "build-args").read_text())

    def test_inexact_go_version_fails_before_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Dockerfile").write_text("FROM alpine@sha256:" + "a" * 64 + "\n")
            (root / "go.mod").write_text("go 1.27\n")
            with self.assertRaises(subprocess.CalledProcessError):
                self.build(root)
            self.assertFalse((root / "build-args").exists())

    @staticmethod
    def build(root):
        docker = """
        docker() {
          if [ "$1 $2" = 'buildx build' ]; then
            printf '%s\\n' "$@" > build-args
          elif [[ "$*" == *Architecture* ]]; then
            echo arm64
          else
            echo 1000
          fi
        }
        """
        run(
            ["bash", "-euo", "pipefail", "-c", textwrap.dedent(docker) + program("Build without publication")],
            root,
            SOURCE_SHA="b" * 40,
            GITHUB_REPOSITORY="daybreaker-labs/explicavet-api",
            TAG="sha-" + "b" * 40,
        )


class PublicationTests(unittest.TestCase):
    def test_publish_reads_machine_json_with_old_buildx(self):
        docker = r"""
        docker() {
          if [[ "$*" == "buildx imagetools inspect "* ]]; then
            if [[ " $* " == *" --raw "* ]]; then
              printf '{"manifests":[{"digest":"%s","platform":{"os":"linux","architecture":"arm64"}}]}\n' "$ARM_DIGEST"
            elif [[ " $* " == *" --format "* ]]; then
              format="${!#}"
              value="$DIGEST"
              if [[ "$4" == *-arm64 ]]; then value="$ARM_DIGEST"; fi
              # Buildx 0.23 treats this prefix as a human-readable summary.
              if [[ "$format" == '{{.Manifest'* ]]; then
                printf 'Name: image\nMediaType: index\nDigest: %s\n' "$value"
              else
                printf '{"digest":"%s"}\n' "$value"
              fi
            else
              echo 'not found' >&2
              return 1
            fi
          elif [[ "$*" == 'tag '* || "$*" == 'push '* || "$*" == 'buildx imagetools create '* ]]; then
            return 0
          else
            echo "Unexpected Docker command: $*" >&2
            return 1
          fi
        }
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            digest = "sha256:" + "b" * 64
            run(
                ["bash", "-euo", "pipefail", "-c", textwrap.dedent(docker) + program("Publish unused tags and verify the index")],
                root,
                IMAGE="fra.ocir.io/frsgxpf5mpny/explicavet-api",
                SOURCE_SHA="a" * 40,
                TAG="v1.2.3",
                DIGEST=digest,
                ARM_DIGEST="sha256:" + "a" * 64,
                RUNNER_TEMP=str(root),
                GITHUB_OUTPUT=str(root / "output"),
                GITHUB_STEP_SUMMARY=str(root / "summary"),
            )
            self.assertEqual((root / "output").read_text(), "digest=" + digest + "\n")


class PromotionTests(unittest.TestCase):
    def promote(self, missing_pin=False):
        calls = []

        def request(request, timeout):
            path = request.full_url.split("daybreaker-gitops/", 1)[1]
            body = None if request.data is None else json.loads(request.data)
            calls.append((path, body))
            if path == "git/ref/heads/main":
                result = {"object": {"sha": "base"}}
            elif path.startswith("contents/"):
                content = "          image: fra.ocir.io/frsgxpf5mpny/explicavet-api@sha256:" + "a" * 64
                if missing_pin and "migrate-job.yaml" in path:
                    content = "unrelated: value"
                result = {"content": base64.b64encode(content.encode()).decode()}
            elif path == "git/commits/base":
                result = {"tree": {"sha": "old-tree"}}
            elif path == "pulls":
                result = {"html_url": "https://github.com/daybreaker-labs/daybreaker-gitops/pull/1"}
            else:
                result = {"sha": "new-object"}
            return io.BytesIO(json.dumps(result).encode())

        source = program("Open a draft GitOps digest change").split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    "IMAGE": "fra.ocir.io/frsgxpf5mpny/explicavet-api",
                    "IMAGE_NAME": "explicavet-api",
                    "DIGEST": "sha256:" + "b" * 64,
                    "TAG": "v1.2.3",
                    "SOURCE_SHA": "c" * 40,
                    "GH_TOKEN": "fixture",
                    "GITHUB_RUN_ID": "123",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_STEP_SUMMARY": str(Path(directory) / "summary"),
                },
            ),
            patch("urllib.request.urlopen", side_effect=request),
        ):
            if missing_pin:
                with self.assertRaises(AssertionError):
                    exec(compile(source, str(WORKFLOW), "exec"), {})  # noqa: S102 - execute the reviewed workflow program under a fake API
            else:
                exec(compile(source, str(WORKFLOW), "exec"), {})  # noqa: S102 - execute the reviewed workflow program under a fake API
        return calls

    def test_both_pins_change_in_one_commit_and_pr_stays_draft(self):
        calls = dict(self.promote())
        self.assertEqual(len(calls["git/trees"]["tree"]), 2)
        for blob in calls["git/trees"]["tree"]:
            self.assertIn("@sha256:" + "b" * 64, blob["content"])
        self.assertEqual(calls["git/commits"]["parents"], ["base"])
        self.assertTrue(calls["pulls"]["draft"])
        self.assertFalse(any("merge" in path for path in calls))

    def test_missing_pin_stops_before_any_write(self):
        self.assertTrue(all(body is None for _, body in self.promote(missing_pin=True)))


if __name__ == "__main__":
    unittest.main()
