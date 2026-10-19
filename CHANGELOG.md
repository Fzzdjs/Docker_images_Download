# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/).

## [2.0.0] - 2026-10-19

First public release under the name **Docker_images_Download**
(previously developed as the private skill `docker-image-fetch`).

### Added

- **Self-contained repository**: the six dispatch workflows now live in this repo,
  so a fork works on its own without an external builder repository.
- **Automatic dispatch-target detection**: `--repo` falls back to the git remote of
  the current checkout, then to an installed `.repo` marker, then to upstream.
  Running the script from your own fork now does the right thing by default.
- **`--check`**: diagnoses token, repository write permission, Actions enablement
  and workflow presence without triggering a run.
- **GitHub CLI token support**: `gh auth login` is enough, no environment variable
  or token file required.
- **`scripts/install_skill.py`**: installs the project as a skill for WorkBuddy,
  Claude Code, or a custom skills directory.
- **Zero-dependency test suite** plus CI on Ubuntu and Windows across Python
  3.9 / 3.12 / 3.13.
- Bilingual documentation, architecture notes, issue templates, Dependabot config.

### Changed

- Release tags renamed to `DockerImages-AMD64` / `-ARM64` / `-ARM32`.
- CLI gained `--version`; usage errors are reported by the argument parser instead
  of a traceback.
- Workflow comments, input descriptions and log text are English; the tool itself
  targets users behind the GFW, so console output stays Chinese.
- Downloads account progress in 256 KiB pieces instead of whole 8 MB chunks, so the
  progress bar moves continuously rather than in jumps.
- Chunk size is now adaptive: small assets (2-8 MB) are split into 1 MB ranges as
  well, because throttling applies per connection, not per file. A 3.5 MB release
  asset went from ~11 KB/s single-stream to four parallel ranges.

### Fixed

- Artifact download no longer sends the token across the 302 redirect to Azure Blob
  (that produced `401 InvalidAuthenticationInfo`).
- Release mode filters assets by the requested image names, so earlier runs no longer
  cause unrelated archives to be downloaded again.
- Non-TTY progress output always ends with a 100% line, so redirected logs do not
  stop at 90%.

### Security

- Minimal `permissions` per workflow: `contents: read` for artifact runs,
  `contents: write` only for the release variants.
- No token is ever written to disk.

## [1.2.0] - 2026-10-19

- Added `progress.py`: adaptive progress bar, sliding-window speed, ETA, spinner.
- Added `--no-progress`; `--json` disables progress automatically.
- Parallel downloader reports incremental progress.

## [1.1.0] - 2026-10-19

- Added 16-way Range downloads (`parallel_dl.py`), lifting throughput from
  ~32 KB/s to ~0.7 MB/s on typical Chinese broadband.
- Release mode filters assets by image name.

## [1.0.0] - 2026-10-19

- Initial working version: trigger workflow, wait for the run, download the artifact,
  optionally `docker load`.
