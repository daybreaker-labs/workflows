"""Exercise the reusable workflow's release gates and promotion program offline."""

import base64
from datetime import datetime
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
                    "PROMOTION_OVERLAY": "",
                    "MIGRATION_JOB": "true",
                    "UPDATE_VERSION_LABEL": "false",
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

    def test_staging_develop_push_can_publish_and_promote(self):
        result = self.verify(event="push", promote="true", PROMOTION_OVERLAY="daybreaker-02",
                             IMAGE_NAME="opensourceradar-api", GITHUB_REPOSITORY="daybreaker-labs/opensourceradar-api",
                             MIGRATION_JOB="false", UPDATE_VERSION_LABEL="true")
        self.assertEqual(result["tag"], "sha-" + result["sha"])

    def test_staging_source_gate_rejects_wrong_target_ref_event_and_inputs(self):
        env = dict(event="push", promote="true", PROMOTION_OVERLAY="daybreaker-02",
                   IMAGE_NAME="opensourceradar-api", GITHUB_REPOSITORY="daybreaker-labs/opensourceradar-api",
                   MIGRATION_JOB="false", UPDATE_VERSION_LABEL="true")
        for change in ({"PROMOTION_OVERLAY": "daybreaker-01"}, {"ref": "refs/heads/main"},
                       {"ref": "refs/tags/v1.2.3"}, {"event": "pull_request"},
                       {"MIGRATION_JOB": "true"}, {"UPDATE_VERSION_LABEL": "false"},
                       {"IMAGE_NAME": "explicavet-api", "GITHUB_REPOSITORY": "daybreaker-labs/explicavet-api"}):
            with self.subTest(change=change), self.assertRaises(subprocess.CalledProcessError):
                self.verify(**{**env, **change})
        with self.assertRaises(subprocess.CalledProcessError):
            self.verify(event="push")

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

    def test_release_metadata_matches_labels(self):
        for tag, version in (("v1.2.3", "1.2.3"), ("sha-" + "b" * 40, "dev")):
            with self.subTest(tag=tag), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "Dockerfile").write_text("FROM scratch\n")
                self.build(root, tag=tag)
                arguments = (root / "build-args").read_text().splitlines()
                self.assertIn("GIT_COMMIT=" + "b" * 40, arguments)
                self.assertIn("VCS_REF=" + "b" * 40, arguments)
                self.assertIn("VERSION=" + version, arguments)
                self.assertIn("org.opencontainers.image.version=" + tag, arguments)
                build_time = next(arg.removeprefix("BUILD_TIME=") for arg in arguments if arg.startswith("BUILD_TIME="))
                self.assertEqual(datetime.strptime(build_time, "%Y-%m-%dT%H:%M:%SZ").isoformat() + "Z", build_time)
                self.assertIn("org.opencontainers.image.created=" + build_time, arguments)

    @staticmethod
    def build(root, tag="sha-" + "b" * 40):
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
            TAG=tag,
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


PIN = "          image: fra.ocir.io/frsgxpf5mpny/explicavet-api@sha256:" + "a" * 64


def deployment(pod_label="        app.kubernetes.io/version: v1.1.1", metadata_label=None):
    lines = ["apiVersion: apps/v1", "kind: Deployment", "metadata:", "  name: explicavet-api"]
    if metadata_label:
        lines += ["  labels:", "    app.kubernetes.io/name: explicavet-api", metadata_label]
    lines += [
        "spec:",
        "  selector:",
        "    matchLabels: {app.kubernetes.io/name: explicavet-api}",
        "  template:",
        "    metadata:",
        "      labels:",
        "        app.kubernetes.io/name: explicavet-api",
    ]
    if pod_label:
        lines.append(pod_label)
    lines += ["    spec:", "      containers:", "        - name: api", PIN, ""]
    return "\n".join(lines)


class PromotionTests(unittest.TestCase):
    def promote(
        self,
        missing_pin=False,
        migration_job="true",
        invalid_file="migrate-job.yaml",
        duplicate_pin=False,
        update_version_label="false",
        content=None,
        invalid=False,
    ):
        calls = []

        def request(request, timeout):
            path = request.full_url.split("daybreaker-gitops/", 1)[1]
            body = None if request.data is None else json.loads(request.data)
            calls.append((path, body))
            if path == "git/ref/heads/main":
                result = {"object": {"sha": "base"}}
            elif path.startswith("contents/"):
                file_content = content if content is not None and "deployment.yaml" in path else PIN
                if invalid_file in path:
                    if missing_pin:
                        file_content = "unrelated: value"
                    elif duplicate_pin:
                        file_content += "\n" + file_content
                result = {"content": base64.b64encode(file_content.encode()).decode()}
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
                    "MIGRATION_JOB": migration_job,
                    "UPDATE_VERSION_LABEL": update_version_label,
                    "GH_TOKEN": "fixture",
                    "GITHUB_RUN_ID": "123",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_STEP_SUMMARY": str(Path(directory) / "summary"),
                },
            ),
            patch("urllib.request.urlopen", side_effect=request),
        ):
            if missing_pin or duplicate_pin or invalid:
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

    def test_single_pin_never_reads_migrate_job_and_pr_stays_draft(self):
        calls = dict(self.promote(migration_job="false"))
        self.assertFalse(any("migrate-job.yaml" in path for path in calls))
        blobs = calls["git/trees"]["tree"]
        self.assertEqual(len(blobs), 1)
        self.assertEqual(blobs[0]["path"], "components/explicavet-api/base/deployment.yaml")
        self.assertIn("@sha256:" + "b" * 64, blobs[0]["content"])
        self.assertEqual(calls["git/commits"]["parents"], ["base"])
        self.assertTrue(calls["pulls"]["draft"])

    def test_invalid_selected_pin_stops_before_any_write(self):
        for migration_job, filename in (("true", "migrate-job.yaml"), ("true", "deployment.yaml"), ("false", "deployment.yaml")):
            for invalid in ("missing_pin", "duplicate_pin"):
                with self.subTest(migration_job=migration_job, filename=filename, invalid=invalid):
                    calls = self.promote(migration_job=migration_job, invalid_file=filename, **{invalid: True})
                    self.assertTrue(all(body is None for _, body in calls))

    def test_version_label_changes_with_the_pin_when_enabled(self):
        source = deployment(metadata_label="    app.kubernetes.io/version: \"v1.1.1\" # release")
        calls = dict(self.promote(update_version_label="true", content=source, migration_job="false"))
        blob = calls["git/trees"]["tree"][0]["content"]
        expected = source.replace("v1.1.1", "v1.2.3").replace("a" * 64, "b" * 64)
        self.assertEqual(blob, expected)
        self.assertIn("Set app.kubernetes.io/version to v1.2.3.", calls["pulls"]["body"])
        self.assertIn("Roll back the promoted image pins and version label to their previous values;", calls["pulls"]["body"])

    def test_leading_document_marker_is_accepted(self):
        source = "---\n" + deployment()
        calls = dict(self.promote(update_version_label="true", content=source, migration_job="false"))
        self.assertIn("version: v1.2.3", calls["git/trees"]["tree"][0]["content"])

    def test_version_label_stays_untouched_when_disabled(self):
        source = deployment(metadata_label="    app.kubernetes.io/version: v1.1.1")
        calls = dict(self.promote(content=source, migration_job="false"))
        blob = calls["git/trees"]["tree"][0]["content"]
        self.assertEqual(blob, source.replace("a" * 64, "b" * 64))
        self.assertNotIn("app.kubernetes.io/version to", calls["pulls"]["body"])
        self.assertIn("Roll back the promoted image pins to their previous digest;", calls["pulls"]["body"])

    def test_version_label_keeps_single_quotes_and_ignores_comments_and_field_refs(self):
        source = deployment(pod_label="        app.kubernetes.io/version: 'v1.1.1'").replace(
            "      containers:",
            "      # app.kubernetes.io/version: set by promotion\n      containers:",
        ).replace(
            PIN,
            PIN + "\n          env:\n            - name: OTEL_SERVICE_VERSION\n              valueFrom:\n"
            "                fieldRef:\n                  fieldPath: metadata.labels['app.kubernetes.io/version']",
        )
        calls = dict(self.promote(update_version_label="true", content=source, migration_job="false"))
        blob = calls["git/trees"]["tree"][0]["content"]
        self.assertEqual(blob, source.replace("v1.1.1", "v1.2.3").replace("a" * 64, "b" * 64))

    def test_version_label_never_touches_the_migration_job(self):
        calls = dict(self.promote(update_version_label="true", content=deployment()))
        blobs = {blob["path"]: blob["content"] for blob in calls["git/trees"]["tree"]}
        self.assertEqual(blobs["components/explicavet-api/base/migrate-job.yaml"], PIN.replace("a" * 64, "b" * 64))
        self.assertIn("version: v1.2.3", blobs["components/explicavet-api/base/deployment.yaml"])

    def test_missing_or_misplaced_version_label_stops_before_any_write(self):
        cases = {
            "missing pod label": deployment(pod_label=None),
            "metadata label only": deployment(pod_label=None, metadata_label="    app.kubernetes.io/version: v1.1.1"),
            "flow style": deployment(pod_label="        {app.kubernetes.io/version: v1.1.1}"),
            "empty value": deployment(pod_label="        app.kubernetes.io/version:"),
            "value on the next line": deployment(pod_label="        app.kubernetes.io/version:\n          v1.1.1"),
            "no space after colon": deployment(pod_label="        app.kubernetes.io/version:v1.1.1"),
            "block scalar": deployment(pod_label="        app.kubernetes.io/version: >-\n          v1.1.1"),
            "multi-line flow map": deployment(pod_label="        app.kubernetes.io/version: v1.1.1,"),
            "second document": deployment() + "---\nkind: Service\nmetadata:\n  labels:\n    app.kubernetes.io/version: v1.1.1\n",
            "duplicate": deployment(pod_label="        app.kubernetes.io/version: v1.1.1\n        app.kubernetes.io/version: v1.1.1"),
            "selector label": deployment().replace(
                "matchLabels: {app.kubernetes.io/name: explicavet-api}",
                "matchLabels:\n      app.kubernetes.io/version: v1.1.1",
            ),
        }
        for name, source in cases.items():
            with self.subTest(name):
                calls = self.promote(update_version_label="true", content=source, migration_job="false", invalid=True)
                self.assertTrue(all(body is None for _, body in calls))

    def test_update_version_label_input_defaults_to_false_and_reaches_promotion(self):
        workflow = WORKFLOW.read_text()
        self.assertIn("      update-version-label:\n        description: Also set app.kubernetes.io/version in deployment.yaml to the release tag\n        type: boolean\n        default: false", workflow)
        self.assertIn("UPDATE_VERSION_LABEL: ${{ inputs.update-version-label }}", workflow)

    def test_migration_job_input_defaults_to_true_and_reaches_promotion(self):
        workflow = WORKFLOW.read_text()
        self.assertIn("      migration-job:\n        description: Also promote the migrate-job.yaml image pin\n        type: boolean\n        default: true", workflow)
        self.assertIn("MIGRATION_JOB: ${{ inputs.migration-job }}", workflow)


class StagingPromotionTests(unittest.TestCase):
    image = "fra.ocir.io/frsgxpf5mpny/opensourceradar-api"
    source_sha = "c" * 40
    kustomization = "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nimages:\n  - name: " + image + "\n    digest: sha256:" + "a" * 64 + "\n"
    staging = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: api\ndata:\n  ENVIRONMENT: staging\n---\n" + deployment(pod_label="        app.kubernetes.io/version: sha-" + "a" * 40) + "---\nkind: Cluster\nmetadata:\n  name: database\n"

    def promote(self, *, files=None, source_sha=None, source_sequence=None, previous=None, invalid=False, **env):
        files = files or {"kustomization.yaml": self.kustomization, "staging.yaml": self.staging}
        calls = []
        sources = iter(source_sequence) if source_sequence else None

        def request(request, timeout):
            path = request.full_url.removeprefix("https://api.github.com/")
            body = None if request.data is None else json.loads(request.data)
            calls.append((path, body, request.get_method()))
            path = path.split("/", 3)[-1]
            if path == "git/ref/heads/develop":
                self.assertEqual(request.headers["Authorization"], "Bearer source-fixture")
                result = {"object": {"sha": next(sources) if sources else source_sha or self.source_sha}}
            elif path == "git/ref/heads/main":
                result = {"object": {"sha": "base"}}
            elif path.startswith("contents/"):
                filename = path.split("?")[0].rsplit("/", 1)[1]
                result = {"content": base64.b64encode(files[filename].encode()).decode()}
            elif path == "git/commits/base":
                result = {"tree": {"sha": "old-tree"}}
            elif path == "pulls":
                result = {"html_url": "https://github.com/daybreaker-labs/daybreaker-gitops/pull/1000", "number": 1000}
            elif path.startswith("pulls?"):
                page = int(path.rsplit("=", 1)[1])
                result = (previous or [])[100 * (page - 1):100 * page]
            else:
                result = {"sha": "new-object"}
            return io.BytesIO(json.dumps(result).encode())

        source = program("Open a draft GitOps digest change").split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "IMAGE": self.image, "IMAGE_NAME": "opensourceradar-api", "DIGEST": "sha256:" + "b" * 64,
            "TAG": "sha-" + self.source_sha, "SOURCE_SHA": self.source_sha, "MIGRATION_JOB": "false",
            "UPDATE_VERSION_LABEL": "true", "PROMOTION_OVERLAY": "daybreaker-02",
            "GH_TOKEN": "deploy-fixture", "SOURCE_TOKEN": "source-fixture",
            "GITHUB_REPOSITORY": "daybreaker-labs/opensourceradar-api", "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "1", "GITHUB_STEP_SUMMARY": str(Path(directory) / "summary"), **env,
        }), patch("urllib.request.urlopen", side_effect=request):
            if invalid:
                with self.assertRaises(AssertionError):
                    exec(compile(source, str(WORKFLOW), "exec"), {})
            else:
                try:
                    exec(compile(source, str(WORKFLOW), "exec"), {})
                except SystemExit as error:
                    self.assertEqual(error.code, 0)
        return calls

    def test_staging_changes_only_digest_and_label_in_ready_pr(self):
        calls = {path.split("/", 3)[-1]: body for path, body, _ in self.promote()}
        blobs = {blob["path"]: blob["content"] for blob in calls["git/trees"]["tree"]}
        directory = "components/opensourceradar-api/overlays/daybreaker-02/"
        self.assertEqual(blobs, {directory + "kustomization.yaml": self.kustomization.replace("a" * 64, "b" * 64),
                                 directory + "staging.yaml": self.staging.replace("sha-" + "a" * 40, "sha-" + self.source_sha)})
        self.assertFalse(calls["pulls"]["draft"])
        self.assertTrue(calls["pulls"]["head"].startswith("staging-promotion/opensourceradar-api/"))

    def test_stale_source_skips_before_writes(self):
        calls = self.promote(source_sha="d" * 40)
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(method == "GET" for _, _, method in calls))

    def test_source_advance_before_pr_skips_creation(self):
        calls = self.promote(source_sequence=[self.source_sha, "d" * 40])
        self.assertFalse(any(path.endswith("/pulls") for path, _, _ in calls))
        self.assertFalse(any(method == "PATCH" for _, _, method in calls))

    def test_source_advance_after_pr_closes_own_pr_only(self):
        calls = self.promote(source_sequence=[self.source_sha, self.source_sha, "d" * 40])
        closes = [(path.rsplit("/", 1)[1], body) for path, body, method in calls if method == "PATCH"]
        self.assertEqual(closes, [("1000", {"state": "closed"})])

    def test_source_advance_during_supersession_cannot_close_another_pr(self):
        previous = [{"number": 1, "head": {"ref": "staging-promotion/opensourceradar-api/old",
                     "repo": {"full_name": "daybreaker-labs/daybreaker-gitops"}},
                     "user": {"login": "daybreaker-gitops-deploy[bot]"}}]
        previous.append({**previous[0], "number": 2})
        for reads, expected in ((3, ["1000"]), (4, ["1", "1000"])):
            with self.subTest(reads=reads):
                calls = self.promote(source_sequence=[self.source_sha] * reads + ["d" * 40], previous=previous)
                closes = [path.rsplit("/", 1)[1] for path, _, method in calls if method == "PATCH"]
                self.assertEqual(closes, expected)

    def test_staging_rejects_invalid_manifest_before_writes(self):
        for files in (
            {"kustomization.yaml": self.kustomization.replace("digest:", "tag:"), "staging.yaml": self.staging},
            {"kustomization.yaml": self.kustomization + "    digest: sha256:" + "a" * 64, "staging.yaml": self.staging},
            {"kustomization.yaml": self.kustomization, "staging.yaml": self.staging.replace("version: sha-", "version: v")},
            {"kustomization.yaml": self.kustomization, "staging.yaml": self.staging + "app.kubernetes.io/version: sha-" + "a" * 40},
            {"kustomization.yaml": self.kustomization, "staging.yaml": self.staging.replace("  template:", "  selector:")},
        ):
            with self.subTest(files=files):
                self.assertTrue(all(method == "GET" for _, _, method in self.promote(files=files, invalid=True)))

    def test_closes_only_prior_bot_staging_prs_and_paginates_before_mutation(self):
        def pull(number, branch="staging-promotion/opensourceradar-api/old", author="daybreaker-gitops-deploy[bot]"):
            return {"number": number, "head": {"ref": branch, "repo": {"full_name": "daybreaker-labs/daybreaker-gitops"}}, "user": {"login": author}}
        fork = pull(103)
        fork["head"]["repo"]["full_name"] = "fork/repository"
        previous = [pull(i) for i in range(100)] + [pull(100), pull(1000), pull(1001), pull(101, branch="promote/release"), pull(102, author="person"), fork]
        calls = self.promote(previous=previous)
        closed = [path.rsplit("/", 1)[1] for path, body, method in calls if method == "PATCH"]
        self.assertEqual(closed, [str(i) for i in range(101)])
        page_two = next(i for i, (path, _, _) in enumerate(calls) if path.endswith("page=2"))
        first_close = next(i for i, (_, _, method) in enumerate(calls) if method == "PATCH")
        self.assertLess(page_two, first_close)


if __name__ == "__main__":
    unittest.main()
