# Docker_images_Download

[![Release](https://img.shields.io/github/v/release/Fzzdjs/Docker_images_Download?color=blue)](https://github.com/Fzzdjs/Docker_images_Download/releases)
[![License](https://img.shields.io/github/license/Fzzdjs/Docker_images_Download)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-informational)](https://www.python.org/)

**Pull Docker images through GitHub Actions when your network cannot reach the registry.**

The script dispatches a workflow on a GitHub-hosted runner, which runs
`docker pull` + `docker save | gzip`, then downloads the archive back and hands it
to `docker load`. No server, no mirror service, no third-party proxy. Works for any
public registry: `docker.io`, `ghcr.io`, `quay.io`, `registry.k8s.io`,
`mcr.microsoft.com`, `nvcr.io`.

[中文说明](README.md) · [Troubleshooting](docs/troubleshooting.md) · [Architecture](docs/architecture.md)

> CLI output is in Chinese. Exit codes and `--json` output are locale-independent.

## Quick start

```bash
# 1) Fork this repository (you need Actions write access to your own fork)
# 2) Authenticate: either log in with the GitHub CLI, or export a token
gh auth login
#    export GITHUB_TOKEN=ghp_xxx

git clone https://github.com/<you>/Docker_images_Download.git
cd Docker_images_Download

python scripts/fetch_image.py nginx:alpine --out ./images --load
```

Token scope: classic `public_repo` + **`workflow`**; fine-grained
Contents = Read, Actions = **Read and write**. The Actions write permission is
mandatory — a read-only token always fails with HTTP 403.

The script detects the git remote of the current checkout and dispatches to **your
fork**, falling back to the upstream repo when it cannot detect one. Override with
`--repo owner/name` or the `DID_REPO` environment variable.

## Examples

```bash
python scripts/fetch_image.py nginx:alpine
python scripts/fetch_image.py redis:7 postgres:16-alpine --out ./images
python scripts/fetch_image.py registry.k8s.io/pause:3.10
python scripts/fetch_image.py alpine:latest --platform arm64
python scripts/fetch_image.py my/app:v1 --mode release      # public release link
python scripts/fetch_image.py --check                       # diagnose setup only
python scripts/fetch_image.py nginx:alpine --json
```

## Options

| Flag | Default | Meaning |
|---|---|---|
| `--platform` | `amd64` | `amd64` / `arm64` / `arm32` |
| `--mode` | `auto` | `auto` = artifact (large limit); `release` = release asset (public link, 2 GiB cap) |
| `--repo` | auto-detected | dispatch target `owner/name` |
| `--ref` | current branch → `main` | branch to dispatch |
| `--out` | `.` | output directory |
| `--load` | off | run `docker load` afterwards |
| `--timeout` | `1800` | seconds to wait for the run |
| `--retention-days` | `1` | artifact retention (1–90) |
| `--connections` | `16` | parallel Range connections |
| `--no-progress` | off | disable progress rendering |
| `--check` | off | report token/repo/workflow readiness |
| `--json` | off | machine-readable summary |

## Why parallel downloads

Artifacts are served from Azure Blob. From mainland China a single connection is
typically throttled to 30–80 KB/s — 5–9 hours for a 1 GB image. Sixteen parallel
Range requests reach 0.7–1.5 MB/s, so the same image takes 15–25 minutes. The
token is deliberately **not** forwarded across the 302 redirect, otherwise Blob
rejects the request with `401 InvalidAuthenticationInfo`.

## Limitations

- Public images only. Private registries need `docker login` wired into the workflow.
- Hosted runners have ~14 GB of disk; very large images (> 5 GB) may not fit.
- Release assets are capped at 2 GiB by GitHub; use the default artifact mode for
  bigger images.
- One run takes 1–3 minutes, so this is not a high-frequency mirror. For team-wide
  continuous pulls, run your own registry cache or a self-hosted runner.

## Install as an agent skill

```bash
python scripts/install_skill.py --target claude      # ~/.claude/skills/Docker_images_Download
python scripts/install_skill.py --target workbuddy   # ~/.workbuddy/skills/Docker_images_Download
```

## Test

```bash
python -m unittest discover -s tests -v
```

## License

[MIT](LICENSE) © 2026 Fzzdjs
