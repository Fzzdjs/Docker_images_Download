#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Dependency-free smoke tests.

Run:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import fetch_image as fi  # noqa: E402
import gh_client  # noqa: E402
import progress as ui  # noqa: E402

try:                      # optional, only used for workflow schema tests
    import yaml
except ImportError:       # pragma: no cover
    yaml = None


class ImageNameTest(unittest.TestCase):
    def test_norm_image_strips_registry_prefixes(self):
        self.assertEqual(fi.norm_image("docker.io/library/nginx:alpine"), "library/nginx:alpine")
        self.assertEqual(fi.norm_image("index.docker.io/nginx"), "nginx")
        self.assertEqual(fi.norm_image("https://ghcr.io/a/b:t"), "ghcr.io/a/b:t")
        self.assertEqual(fi.norm_image("  nginx:alpine  "), "nginx:alpine")

    def test_safe_name_matches_workflow_rule(self):
        self.assertEqual(fi.safe_name("registry.k8s.io/pause:3.10"),
                         "registry.k8s.io_pause_3.10")
        self.assertEqual(fi.safe_name("ghcr.io/tencentcloud/octop@sha256:abc"),
                         "ghcr.io_tencentcloud_octop_sha256_abc")

    def test_human(self):
        self.assertEqual(fi.human(512), "512.00 B")
        self.assertEqual(fi.human(1536), "1.50 KB")


class RemoteParseTest(unittest.TestCase):
    def test_https(self):
        self.assertEqual(fi.parse_remote("https://github.com/Fzzdjs/Docker_images_Download.git"),
                         "Fzzdjs/Docker_images_Download")
        self.assertEqual(fi.parse_remote("https://github.com/Fzzdjs/repo"), "Fzzdjs/repo")

    def test_ssh(self):
        self.assertEqual(fi.parse_remote("git@github.com:Fzzdjs/repo.git"), "Fzzdjs/repo")
        self.assertEqual(fi.parse_remote("ssh://git@github.com/Fzzdjs/repo.git"), "Fzzdjs/repo")

    def test_local_path_is_rejected(self):
        self.assertIsNone(fi.parse_remote("D:/repos/foo"))
        self.assertIsNone(fi.parse_remote("/home/user/repo"))
        self.assertIsNone(fi.parse_remote(""))


class RepoDetectionTest(unittest.TestCase):
    def test_env_overrides_everything(self):
        with mock.patch.dict(os.environ, {"DID_REPO": "someone/fork", "DID_REF": "dev"}):
            repo, ref, source = fi.detect_repo()
        self.assertEqual(repo, "someone/fork")
        self.assertEqual(ref, "dev")
        self.assertIn("DID_REPO", source)


class TokenTest(unittest.TestCase):
    def test_explicit_token_wins(self):
        token, source = gh_client.resolve_token("abc123")
        self.assertEqual(token, "abc123")
        self.assertEqual(source, "--token")

    def test_env_token(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "env-token"}, clear=False):
            with mock.patch.object(gh_client, "_gh_cli_token", return_value=None):
                token, source = gh_client.resolve_token(None)
        self.assertEqual(token, "env-token")
        self.assertIn("GITHUB_TOKEN", source)


class ProgressTest(unittest.TestCase):
    def test_non_tty_emits_deciles_and_final_line(self):
        buf = io.StringIO()
        bar = ui.ProgressBar(1000, label="dl", stream=buf, enabled=True)
        for done in (100, 250, 500, 999, 1000):
            bar.update(done, force=True)
        bar.close()
        out = buf.getvalue()
        self.assertIn("0.0%", out)
        self.assertIn("100.0%", out)
        self.assertNotIn("\r", out)          # no carriage returns when redirected

    def test_human_and_duration(self):
        self.assertEqual(ui.human(1024), "1.0 KB")
        self.assertEqual(ui.duration(65), "01:05")
        self.assertEqual(ui.duration(3661), "1:01:01")


class WorkflowTest(unittest.TestCase):
    FILES = ["amd64.yml", "arm64.yml", "arm32.yml",
             "amd64-release.yml", "arm64-release.yml", "arm32-release.yml"]

    def read(self, name):
        with open(os.path.join(ROOT, ".github", "workflows", name), encoding="utf-8") as fh:
            return fh.read()

    def test_all_workflows_present(self):
        for name in self.FILES:
            self.assertTrue(os.path.isfile(os.path.join(ROOT, ".github", "workflows", name)), name)

    def test_workflows_are_dispatchable_and_pack_images(self):
        for name in self.FILES:
            text = self.read(name)
            with self.subTest(workflow=name):
                self.assertIn("workflow_dispatch", text)
                self.assertIn("docker save", text)
                self.assertIn("docker pull --platform", text)
                self.assertIn("actions/checkout@v7", text)
                self.assertIn("timeout-minutes", text)

    def test_release_workflows_guard_the_2gib_limit(self):
        for name in self.FILES:
            if not name.endswith("-release.yml"):
                continue
            text = self.read(name)
            with self.subTest(workflow=name):
                self.assertIn("2 * 1024 * 1024 * 1024", text)
                self.assertIn("softprops/action-gh-release", text)
                self.assertIn("contents: write", text)

    def test_artifact_name_matches_arch(self):
        for arch in ("amd64", "arm64", "arm32"):
            self.assertIn("name: docker-images-${{ env.ARCH }}", self.read("%s.yml" % arch))


class MetadataTest(unittest.TestCase):
    def test_skill_frontmatter(self):
        with open(os.path.join(ROOT, "SKILL.md"), encoding="utf-8") as fh:
            head = fh.read(600)
        self.assertTrue(head.startswith("---"))
        self.assertIn("name: Docker_images_Download", head)
        self.assertIn("description:", head)

    def test_license_is_mit(self):
        with open(os.path.join(ROOT, "LICENSE"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("MIT License", text)
        self.assertIn("Permission is hereby granted", text)

    def test_cli_help_runs(self):
        # Explicit PIPE + manual decode: text=True relies on the locale encoding,
        # which differs between Linux runners and Windows runners.
        proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "fetch_image.py"), "--help"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        stdout = (proc.stdout or b"").decode("utf-8", "replace")
        stderr = (proc.stderr or b"").decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0, msg=stderr)
        self.assertIn("--check", stdout)
        self.assertIn("--platform", stdout)


@unittest.skipIf(yaml is None, "PyYAML not installed")
class WorkflowSchemaTest(unittest.TestCase):
    """Parses every workflow and checks the contract the CLI relies on."""

    def load(self, name):
        with open(os.path.join(ROOT, ".github", "workflows", name), encoding="utf-8") as fh:
            return yaml.safe_load(fh)

    def test_every_workflow_parses(self):
        for name in sorted(os.listdir(os.path.join(ROOT, ".github", "workflows"))):
            if not name.endswith((".yml", ".yaml")):
                continue
            with self.subTest(workflow=name):
                data = self.load(name)
                self.assertIn("jobs", data)
                # PyYAML maps the bare key `on` to the boolean True.
                triggers = data.get("on", data.get(True))
                self.assertIsNotNone(triggers, name)

    def test_dispatch_inputs_match_the_cli(self):
        for name in WorkflowTest.FILES:
            with self.subTest(workflow=name):
                triggers = self.load(name)
                triggers = triggers.get("on", triggers.get(True))
                dispatch = triggers["workflow_dispatch"]
                self.assertIn("docker_images", dispatch["inputs"])
                self.assertTrue(dispatch["inputs"]["docker_images"]["required"])

    def test_permissions_are_minimal(self):
        for name in WorkflowTest.FILES:
            data = self.load(name)
            with self.subTest(workflow=name):
                expected = "write" if name.endswith("-release.yml") else "read"
                self.assertEqual(data["permissions"]["contents"], expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
