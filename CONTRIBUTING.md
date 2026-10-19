# Contributing

Thanks for helping. This project is small on purpose: standard library only, no
build step, no runtime dependencies.

## Development setup

```bash
git clone https://github.com/<you>/Docker_images_Download.git
cd Docker_images_Download
python -m unittest discover -s tests -v
```

Python 3.8+ is enough to run; CI covers 3.9, 3.12 and 3.13 on Ubuntu and Windows.

## Ground rules

1. **No third-party runtime dependencies.** `scripts/` must run on a bare Python
   install. Test-only dependencies are acceptable.
2. **Never log or persist tokens.** Authentication goes through
   `gh_client.resolve_token`; all auth headers use `add_unredirected_header`.
3. **Keep workflows minimal.** Pin `permissions`, set `timeout-minutes`, and use
   `ubuntu-24.04` explicitly (do not rely on `ubuntu-latest`).
4. **Chinese console output, English code comments.** The tool targets users behind
   the GFW; contributors read English.

## Useful checks before opening a PR

```bash
python -m compileall -q scripts tests
python -m unittest discover -s tests -v
python scripts/fetch_image.py --help
```

If you touched a workflow, also verify its shell blocks parse:

```bash
bash -n <(sed -n '/run: |/,/^      - name:/p' .github/workflows/amd64.yml | sed 's/^          //')
```

## Adding a platform

1. Add an entry to `PLATFORMS` in `scripts/fetch_image.py` (arch, docker platform,
   workflow names, release tag).
2. Add `<arch>.yml` and `<arch>-release.yml` under `.github/workflows/`, following
   the existing pair.
3. Extend `WorkflowTest.FILES` in `tests/test_smoke.py`.
4. Add the row to both READMEs.

## Reporting download-speed data

Speed reports are genuinely useful, because the parallel-download defaults were
tuned from real measurements. Please include: province/city, ISP, advertised
bandwidth, image size, `--connections`, and the average MB/s printed at the end.

## Commit messages

Conventional Commits style, imperative mood:

```
feat(cli): add --check for setup diagnostics
fix(download): refresh signed URL on 403
docs: clarify fine-grained token permissions
```

## License

By contributing you agree your work is released under the [MIT License](LICENSE).
