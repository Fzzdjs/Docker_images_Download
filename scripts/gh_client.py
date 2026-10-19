#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Minimal GitHub REST API client (standard library only).

Two details that are easy to get wrong:

1. **Never forward the Authorization header across a redirect.**
   Artifact downloads answer ``302`` to an Azure Blob URL. If the token is sent
   along, Blob rejects it with ``InvalidAuthenticationInfo`` (HTTP 401). Every
   token header here is therefore added with ``add_unredirected_header``.

2. **Resolve the token from wherever the user already keeps it.**
   Order: explicit ``--token`` -> environment -> ``gh auth token`` ->
   legacy WorkBuddy DPAPI store. See :func:`resolve_token`.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
UA = "Docker_images_Download/2.0 (+https://github.com/Fzzdjs/Docker_images_Download)"

# Legacy store used by the WorkBuddy skill runtime. Optional: absent on most
# machines, and the code below simply skips it when it is not there.
LEGACY_SECRET_DIR = os.path.join(os.path.expanduser("~"), ".workbuddy", "secrets")


def setup_stdout() -> None:
    """Force UTF-8 on stdout/stderr so Chinese logs survive a GBK console."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str, path: str = ""):
        super().__init__("[HTTP %s] %s%s" % (status, message, (" @ " + path) if path else ""))
        self.status = status
        self.message = message
        self.path = path


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop at the 302 so the token never reaches the storage host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _gh_cli_token() -> str | None:
    """Read the token of an already logged-in GitHub CLI, if any."""
    try:
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True,
                              text=True, timeout=8)
    except Exception:
        return None
    token = (proc.stdout or "").strip()
    return token if proc.returncode == 0 and token else None


def _legacy_dpapi_token(name: str = "github-pat") -> str | None:
    """Legacy WorkBuddy DPAPI store (Windows only). Safe to ignore elsewhere."""
    path = os.path.join(LEGACY_SECRET_DIR, name + ".cred")
    if not os.path.exists(path):
        return None
    if LEGACY_SECRET_DIR not in sys.path:
        sys.path.insert(0, LEGACY_SECRET_DIR)
    try:
        import credctl  # type: ignore

        with open(path, encoding="ascii") as fh:
            data = json.loads(credctl.unprotect(base64.b64decode(fh.read())).decode("utf-8"))
        return data.get("secret") or None
    except Exception:
        return None


def resolve_token(explicit: str | None = None, cred_name: str = "github-pat") -> tuple[str, str]:
    """Return ``(token, where_it_came_from)`` or exit with setup guidance."""
    if explicit:
        return explicit.strip(), "--token"

    for var in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT"):
        value = os.environ.get(var)
        if value and value.strip():
            return value.strip(), "environment variable %s" % var

    token = _gh_cli_token()
    if token:
        return token, "GitHub CLI (gh auth token)"

    token = _legacy_dpapi_token(cred_name)
    if token:
        return token, "WorkBuddy encrypted store"

    raise SystemExit(
        "No GitHub token found. Pick one of:\n"
        "  1) GitHub CLI (easiest):  gh auth login\n"
        "  2) Environment variable:  export GITHUB_TOKEN=<token>\n"
        "     (Windows: setx GITHUB_TOKEN <token>)\n"
        "  3) Pass it directly:      --token <token>\n"
        "\n"
        "Token permissions:\n"
        "  fine-grained: Contents = Read, Actions = Read and write\n"
        "  classic:      public_repo + workflow\n"
        "\n"
        "The Actions write permission is mandatory: triggering workflow_dispatch\n"
        "requires it. A read-only token always fails with HTTP 403.\n"
        "Create one at https://github.com/settings/tokens\n")


class GitHub:
    def __init__(self, token: str, timeout: int = 60):
        self.token = token
        self.timeout = timeout

    # ------------------------------------------------------------ transport
    def _open(self, url: str, method: str, body: bytes | None, accept: str):
        req = urllib.request.Request(url, data=body, method=method)
        # unredirected: a 302 to Azure Blob must not carry the token
        req.add_unredirected_header("Authorization", "Bearer " + self.token)
        req.add_header("Accept", accept)
        req.add_header("User-Agent", UA)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        return urllib.request.urlopen(req, timeout=self.timeout)

    def request(self, path: str, method: str = "GET", data=None, raw: bool = False,
                accept: str = "application/vnd.github+json", base: str = API):
        url = path if path.startswith("http") else base + path
        body = json.dumps(data).encode() if data is not None else None
        try:
            with self._open(url, method, body, accept) as r:
                payload = r.read()
                if raw:
                    return r.status, payload
                return r.status, (json.loads(payload.decode() or "{}") if payload.strip() else {})
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            try:
                msg = json.loads(detail).get("message", detail)
            except Exception:
                msg = detail[:200]
            raise GitHubError(e.code, msg, path) from None

    def get(self, path, **kw):
        return self.request(path, "GET", **kw)

    def post(self, path, data=None, **kw):
        return self.request(path, "POST", data=data, **kw)

    def put(self, path, data=None, **kw):
        return self.request(path, "PUT", data=data, **kw)

    def delete(self, path, **kw):
        return self.request(path, "DELETE", **kw)

    # ------------------------------------------------------------ semantic
    def me(self) -> dict:
        return self.get("/user")[1]

    def repo(self, owner: str, name: str) -> dict:
        return self.get("/repos/%s/%s" % (owner, name))[1]

    def workflows(self, owner: str, name: str) -> list:
        _, d = self.get("/repos/%s/%s/actions/workflows" % (owner, name))
        return d.get("workflows", [])

    def actions_permissions(self, owner: str, name: str) -> dict:
        return self.get("/repos/%s/%s/actions/permissions" % (owner, name))[1]

    def dispatch(self, owner: str, name: str, workflow_file: str, ref: str, inputs: dict) -> None:
        """Trigger workflow_dispatch. Success is 204 with an empty body."""
        self.post("/repos/%s/%s/actions/workflows/%s/dispatches" % (owner, name, workflow_file),
                  {"ref": ref, "inputs": inputs})

    def workflow_runs(self, owner: str, name: str, workflow_file: str, per_page: int = 5) -> list:
        _, d = self.get("/repos/%s/%s/actions/workflows/%s/runs?per_page=%d"
                        % (owner, name, workflow_file, per_page))
        return d.get("workflow_runs", [])

    def run(self, owner: str, name: str, run_id: int) -> dict:
        return self.get("/repos/%s/%s/actions/runs/%s" % (owner, name, run_id))[1]

    def run_jobs(self, owner: str, name: str, run_id: int) -> list:
        _, d = self.get("/repos/%s/%s/actions/runs/%s/jobs" % (owner, name, run_id))
        return d.get("jobs", [])

    def run_artifacts(self, owner: str, name: str, run_id: int) -> list:
        _, d = self.get("/repos/%s/%s/actions/runs/%s/artifacts" % (owner, name, run_id))
        return d.get("artifacts", [])

    def artifact_url(self, owner: str, name: str, artifact_id: int) -> str:
        """Resolve the signed Azure Blob URL without downloading the archive.

        Large artifacts must be fetched with concurrent Range requests, and a
        request that follows the 302 would send the token to Blob and get a 401.
        So: exchange the token for the signed URL first, then download it raw.
        The signature has a TTL; call this again when it starts returning 403.
        """
        url = API + "/repos/%s/%s/actions/artifacts/%s/zip" % (owner, name, artifact_id)
        req = urllib.request.Request(url)
        req.add_unredirected_header("Authorization", "Bearer " + self.token)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", UA)
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=self.timeout) as r:
                return r.geturl()
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location")
            if loc:
                return loc
            detail = e.read().decode("utf-8", "replace")
            try:
                msg = json.loads(detail).get("message", detail)
            except Exception:
                msg = detail[:200]
            raise GitHubError(e.code, msg, url) from None

    def release_by_tag(self, owner: str, name: str, tag: str) -> dict:
        return self.get("/repos/%s/%s/releases/tags/%s"
                        % (owner, name, urllib.parse.quote(tag)))[1]
