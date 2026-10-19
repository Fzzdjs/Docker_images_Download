#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Fetch Docker images through GitHub Actions and save them as local tar.gz files.

How it works:
    The script triggers one of the workflows in this repository via
    ``workflow_dispatch``. A GitHub-hosted runner does ``docker pull`` followed
    by ``docker save | gzip``, and the resulting archive is downloaded back to
    your machine. Hosted runners can reach any public registry, so this works
    for docker.io, ghcr.io, quay.io, registry.k8s.io and mcr.microsoft.com even
    when they are unreachable from your network.

Usage:
    python scripts/fetch_image.py nginx:alpine
    python scripts/fetch_image.py nginx:alpine --out ./images --load
    python scripts/fetch_image.py registry.k8s.io/pause:3.10 --platform arm64
    python scripts/fetch_image.py my/app:v1 --mode release   # public download link
    python scripts/fetch_image.py nginx:alpine --check       # verify setup only
    python scripts/fetch_image.py nginx:alpine --json        # machine readable

Exit codes: 0 ok / 1 workflow failed / 2 usage or credentials / 3 download failed
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parallel_dl  # noqa: E402
import progress as ui  # noqa: E402
from gh_client import GitHub, GitHubError, resolve_token, setup_stdout  # noqa: E402

APP_VERSION = "2.0.0"
DEFAULT_REPO = "Fzzdjs/Docker_images_Download"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PLATFORMS = {
    "amd64": {"arch": "amd64", "docker_platform": "linux/amd64",
              "workflow": "amd64.yml", "release_workflow": "amd64-release.yml",
              "release_tag": "DockerImages-AMD64"},
    "arm64": {"arch": "arm64", "docker_platform": "linux/arm64",
              "workflow": "arm64.yml", "release_workflow": "arm64-release.yml",
              "release_tag": "DockerImages-ARM64"},
    "arm32": {"arch": "arm32", "docker_platform": "linux/arm/v7",
              "workflow": "arm32.yml", "release_workflow": "arm32-release.yml",
              "release_tag": "DockerImages-ARM32"},
}


def ts() -> str:
    return time.strftime("%H:%M:%S")


def log(msg: str) -> None:
    print("[%s] %s" % (ts(), msg), flush=True)


def safe_name(image: str) -> str:
    """Same naming rule as the workflows: / : @ become _."""
    return image.replace("/", "_").replace(":", "_").replace("@", "_")


def norm_image(image: str) -> str:
    """Normalise common spellings so docker.io prefixes never reach the runner."""
    img = image.strip()
    for prefix in ("docker.io/", "index.docker.io/", "registry-1.docker.io/",
                   "https://", "http://"):
        if img.startswith(prefix):
            img = img[len(prefix):]
    return img


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.2f %s" % (n, unit)
        n /= 1024.0
    return str(n)


def docker_available() -> bool:
    try:
        r = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


# ------------------------------------------------------------------ repo resolution
def parse_remote(url: str) -> str | None:
    """Turn a git remote URL into ``owner/name``. Returns None if unparseable."""
    url = (url or "").strip()
    if not url:
        return None
    if "://" in url:
        # https://github.com/owner/repo(.git)
        path = "/".join(url.rstrip("/").split("/")[-2:])
    elif "@" in url and ":" in url:
        # scp-like: git@github.com:owner/repo(.git)
        path = url.split(":", 1)[1]
    else:
        # Local path or something unexpected: do not guess.
        return None
    path = path[:-4] if path.endswith(".git") else path
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2:
        return "%s/%s" % (parts[-2], parts[-1])
    return None


def _git(*args: str) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", ROOT] + list(args),
                              capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    out = (proc.stdout or "").strip()
    return out if proc.returncode == 0 and out else None


def detect_repo() -> tuple[str, str, str]:
    """Resolve ``(owner/name, ref, source_description)``.

    Precedence: --repo/--ref > environment > installed config > git checkout >
    upstream default. Detecting the checkout matters because most users run
    this from their own fork, which is the repo their token can dispatch.
    """
    env_repo = (os.environ.get("DID_REPO") or "").strip()
    env_ref = (os.environ.get("DID_REF") or "").strip()
    if env_repo:
        return env_repo, env_ref or "main", "environment DID_REPO"

    cfg = os.path.join(ROOT, ".repo")
    if os.path.isfile(cfg):
        try:
            with open(cfg, encoding="utf-8") as fh:
                value = fh.read().strip()
            if "/" in value:
                return value, env_ref or "main", "installed config .repo"
        except Exception:
            pass

    if os.path.isfile(os.path.join(ROOT, ".github", "workflows", "amd64.yml")):
        remote = _git("remote", "get-url", "origin")
        repo = parse_remote(remote or "")
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        if branch in ("HEAD", ""):
            branch = None
        if repo:
            return repo, env_ref or branch or "main", "git remote of this checkout"

    return DEFAULT_REPO, env_ref or "main", "upstream default"


# ------------------------------------------------------------------ workflows
def ensure_ready(gh: GitHub, owner: str, repo: str, workflow: str) -> None:
    info = gh.repo(owner, repo)
    if not info.get("permissions", {}).get("push"):
        raise SystemExit(
            "令牌对 %s/%s 没有写权限，无法触发 Actions。\n"
            "如果你没有 fork 过本仓库：先 fork，然后用 --repo 你的账号/%s 指定。\n"
            "并用你自己的令牌（需 Actions 写权限）。" % (owner, repo, repo))
    try:
        perms = gh.actions_permissions(owner, repo)
        if not perms.get("enabled", True):
            raise SystemExit("%s/%s 未启用 Actions，请到仓库 Settings → Actions 开启。" % (owner, repo))
    except GitHubError as e:
        if e.status in (403, 404):
            raise SystemExit("无法读取 %s/%s 的 Actions 设置 (HTTP %s)，请确认令牌含 Actions 读权限。"
                             % (owner, repo, e.status))
        raise
    names = {w["path"].split("/")[-1] for w in gh.workflows(owner, repo)}
    if workflow not in names:
        raise SystemExit("仓库 %s/%s 里没有 %s，现有工作流: %s"
                         % (owner, repo, workflow, sorted(names)))


def wait_for_run(gh: GitHub, owner: str, repo: str, workflow: str,
                 baseline_ids: set, timeout: int, show_progress: bool = True) -> dict:
    """Wait for the freshly triggered run to appear and finish."""
    deadline = time.time() + timeout
    spinner = ui.Spinner("等待工作流", enabled=show_progress).start()
    run = None
    while time.time() < deadline:
        runs = gh.workflow_runs(owner, repo, workflow, per_page=10)
        fresh = [r for r in runs if r["id"] not in baseline_ids]
        if fresh:
            run = max(fresh, key=lambda r: r["id"])
            break
        spinner.update("已提交, 等待运行实例创建")
        time.sleep(5)
    if run is None:
        spinner.stop("超时")
        raise SystemExit("触发了工作流但 %d 秒内没有出现新的运行记录。" % timeout)

    spinner.stop("运行已创建")
    log("运行已创建: %s" % run["html_url"])

    spinner = ui.Spinner("工作流运行中", enabled=show_progress).start()
    last = None
    while time.time() < deadline:
        run = gh.run(owner, repo, run["id"])
        status_cn = {"queued": "排队中", "in_progress": "执行中",
                     "completed": "已完成"}.get(run["status"], run["status"])
        if show_progress:
            left = max(0, deadline - time.time())
            spinner.update("%s  %s  (上限还剩 %s)" % (status_cn, run.get("conclusion") or "",
                                                      ui.duration(left)))
        elif run["status"] != last:
            log("  状态: %s" % run["status"])
        last = run["status"]
        if run["status"] == "completed":
            spinner.stop("结束: %s" % run["conclusion"])
            return run
        time.sleep(8)
    spinner.stop("等待超时")
    raise SystemExit("工作流 %d 秒内未结束, 请打开运行页查看: %s" % (timeout, run["html_url"]))


def report_run(gh: GitHub, owner: str, repo: str, run_id: int) -> None:
    for job in gh.run_jobs(owner, repo, run_id):
        mark = {"success": "OK  ", "failure": "FAIL", "skipped": "SKIP"}.get(job.get("conclusion"), "..")
        log("  [%s] %s" % (mark, job["name"]))
        for s in job.get("steps") or []:
            m = {"success": "  ok ", "failure": "  X  ", "skipped": "  -  "}.get(s.get("conclusion"), "  .. ")
            log("      %s %s" % (m, s["name"]))


def run_check(gh: GitHub, owner: str, repo: str, plat: dict) -> int:
    """Print everything that commonly blocks a first run. Does not dispatch."""
    me = gh.me()
    print("账号      : %s" % me.get("login"))
    info = gh.repo(owner, repo)
    perms = info.get("permissions", {})
    print("仓库      : %s/%s  (%s)" % (owner, repo, "private" if info.get("private") else "public"))
    print("默认分支  : %s" % info.get("default_branch"))
    print("push 权限 : %s" % ("有" if perms.get("push") else "无 — 触发 Actions 需要写权限"))

    problems = []
    if not perms.get("push"):
        problems.append("令牌对该仓库没有写权限：请 fork 后用 --repo 指向自己的仓库。")

    try:
        enabled = gh.actions_permissions(owner, repo).get("enabled", True)
    except GitHubError as e:
        enabled = False
        problems.append("读取 Actions 设置失败 (HTTP %s)：令牌可能缺 Actions 读权限。" % e.status)
    print("Actions   : %s" % ("已启用" if enabled else "未启用"))
    if not enabled and not any("Actions 设置失败" in p for p in problems):
        problems.append("Actions 未启用：Settings → Actions → 开启。")

    names = sorted(w["path"].split("/")[-1] for w in gh.workflows(owner, repo))
    print("工作流    : %s" % (", ".join(names) or "（无）"))
    for need in (plat["workflow"], plat["release_workflow"]):
        if need not in names:
            problems.append("缺少工作流 %s：确认分支/仓库是否正确。" % need)

    print()
    if problems:
        for p in problems:
            print("  ✗ %s" % p)
        return 2
    print("  ✓ 配置可用，可以正常触发。")
    return 0


# ------------------------------------------------------------------ download
def fetch_artifact(gh: GitHub, owner: str, repo: str, run_id: int,
                   arch: str, out_dir: str, images: list,
                   connections: int = 16, progress: bool = True) -> list:
    arts = gh.run_artifacts(owner, repo, run_id)
    wanted = "docker-images-%s" % arch
    art = next((a for a in arts if a["name"] == wanted), None)
    if art is None:
        raise RuntimeError("该运行没有名为 %s 的产物 (现有: %s)" % (wanted, [a["name"] for a in arts]))
    if art.get("expired"):
        raise RuntimeError("产物 %s 已过期 (Artifact 保留期到点), 请重跑一次。" % wanted)

    size = int(art["size_in_bytes"])
    log("下载产物 %s (%s) ..." % (wanted, human(size)))
    need = int(size * 2.2)          # zip + extracted tar.gz, with headroom
    free = parallel_dl.free_space(out_dir)
    if free < need:
        log("  警告: %s 剩余 %.1f GB, 本次约需 %.1f GB" % (out_dir, free / 1073741824.0,
                                                           need / 1073741824.0))

    # A single connection is throttled to tens of KB/s from China; fetch in
    # parallel ranges and refresh the signed URL when it expires mid-download.
    zip_path = os.path.join(out_dir, ".%s.zip.part" % wanted)
    try:
        parallel_dl.download(
            gh.artifact_url(owner, repo, art["id"]), zip_path,
            connections=connections,
            refresh=lambda: gh.artifact_url(owner, repo, art["id"]),
            label="下载产物", progress=progress)
    except Exception:
        if os.path.exists(zip_path):
            os.remove(zip_path)
        raise

    saved = []
    names: list = []
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
            members = [n for n in names if n.endswith(".tar.gz")]
            total_bytes = sum(z.getinfo(n).file_size for n in members)
            bar = ui.ProgressBar(total_bytes, label="解包", enabled=progress)
            done = 0
            for name in members:
                base = os.path.basename(name)
                dest = os.path.join(out_dir, base)
                with z.open(name) as src, open(dest, "wb") as fh:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        bar.update(done)
                saved.append(dest)
            bar.close()
            for path in saved:
                log("  解出 %s (%s)" % (path, human(os.path.getsize(path))))
    finally:
        if os.path.exists(zip_path):
            os.remove(zip_path)
    if not saved:
        raise RuntimeError("产物里没有 .tar.gz 文件 (内容: %s)" % names)

    missing = [i for i in images
               if os.path.join(out_dir, "%s-%s.tar.gz" % (safe_name(i), arch)) not in saved]
    if missing:
        log("  注意: 以下镜像未出现在产物中: %s" % ", ".join(missing))
    return saved


def fetch_release(gh: GitHub, owner: str, repo: str, tag: str, arch: str,
                  out_dir: str, images: list, connections: int = 16,
                  progress: bool = True) -> list:
    """Download release assets.

    The release uses a fixed tag, so assets from earlier runs accumulate.
    Filter by the requested image names instead of downloading everything.
    """
    rel = gh.release_by_tag(owner, repo, tag)
    assets = [a for a in rel.get("assets", []) if a["name"].endswith("-%s.tar.gz" % arch)]
    if not assets:
        raise RuntimeError("Release %s 下没有 *-%s.tar.gz 附件" % (tag, arch))

    wanted = {"%s-%s.tar.gz" % (safe_name(i), arch) for i in images}
    picked = [a for a in assets if a["name"] in wanted]
    if picked:
        if len(picked) < len(assets):
            log("  Release 中共有 %d 个附件, 本次只取匹配的 %d 个"
                % (len(assets), len(picked)))
    else:
        # Names may not match after normalisation (e.g. digest in the filename).
        log("  警告: 未按镜像名匹配到附件, 将下载该架构下的全部 %d 个附件" % len(assets))
        picked = assets

    saved = []
    for idx, a in enumerate(picked, 1):
        dest = os.path.join(out_dir, a["name"])
        log("下载 Release 附件 %s (%s) ..." % (a["name"], human(a["size"])))
        prefix = "[%d/%d] " % (idx, len(picked)) if len(picked) > 1 else ""
        parallel_dl.download(a["browser_download_url"], dest, connections=connections,
                             label=prefix + a["name"], progress=progress)
        saved.append(dest)
        log("  已保存 %s" % dest)
    return saved


# ------------------------------------------------------------------ main
def main() -> int:
    setup_stdout()
    ap = argparse.ArgumentParser(
        description="借 GitHub Actions 中转下载无法直连的 Docker 镜像",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("images", nargs="*", help="镜像名, 可多个 (如 nginx:alpine registry.k8s.io/pause:3.10)")
    ap.add_argument("--platform", default="amd64", choices=sorted(PLATFORMS), help="目标平台 (默认 amd64)")
    ap.add_argument("--mode", default="auto", choices=["auto", "artifact", "release"],
                    help="auto=产物(默认, 限额更大); release=发到 Release 生成公开链接(单附件<=2GiB)")
    ap.add_argument("--repo", default=None,
                    help="中转仓库 owner/name (默认: 自动识别当前 checkout, 否则 %s)" % DEFAULT_REPO)
    ap.add_argument("--ref", default=None, help="触发所用的分支 (默认: 当前分支, 否则 main)")
    ap.add_argument("--out", default=".", help="镜像 tar.gz 保存目录 (默认当前目录)")
    ap.add_argument("--timeout", type=int, default=1800, help="等待工作流完成的上限秒数 (默认 1800)")
    ap.add_argument("--retention-days", type=int, default=1, help="产物保留天数 (默认 1)")
    ap.add_argument("--load", action="store_true", help="下载后直接 docker load 导入本地")
    ap.add_argument("--token", default=None, help="GitHub 令牌 (默认取环境变量 / gh CLI)")
    ap.add_argument("--cred-name", default="github-pat", help="旧版加密存储的凭据名 (一般不用)")
    ap.add_argument("--connections", type=int, default=16,
                    help="分片并发数 (默认 16; 国内单连接很慢, 并发可近线性提速)")
    ap.add_argument("--no-progress", action="store_true",
                    help="关闭进度条与旋转指示器 (适合重定向到日志/CI 环境)")
    ap.add_argument("--check", action="store_true", help="只检查令牌/仓库/工作流是否就绪, 不触发运行")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果摘要")
    ap.add_argument("--version", action="version", version="Docker_images_Download %s" % APP_VERSION)
    args = ap.parse_args()

    show_progress = not args.no_progress and not args.json

    if not args.images and not args.check:
        ap.error("至少需要一个镜像名（或用 --check 仅检查配置）")

    env_repo = (os.environ.get("DID_REPO") or "").strip()
    if args.repo:
        repo_full, repo_src = args.repo.strip(), "--repo"
    elif env_repo:
        repo_full, repo_src = env_repo, "环境变量 DID_REPO"
    else:
        repo_full, _, repo_src = detect_repo()

    if "/" not in repo_full:
        raise SystemExit("--repo 需要写成 owner/name 形式")
    owner, repo = repo_full.split("/", 1)

    detected_repo, detected_ref, _ = detect_repo()
    ref = args.ref or (os.environ.get("DID_REF") or "").strip() or (
        detected_ref if detected_repo == repo_full else "main")

    plat = PLATFORMS[args.platform]
    arch = plat["arch"]
    out_dir = os.path.abspath(args.out)

    token, tok_src = resolve_token(args.token, args.cred_name)
    gh = GitHub(token)

    if args.check:
        print("令牌来源  : %s" % tok_src)
        print("中转仓库  : %s @ %s (%s)" % (repo_full, ref, repo_src))
        print()
        return run_check(gh, owner, repo, plat)

    os.makedirs(out_dir, exist_ok=True)
    images = [norm_image(i) for i in args.images]
    mode = "release" if args.mode == "release" else "artifact"
    workflow = plat["release_workflow"] if mode == "release" else plat["workflow"]

    log("账号      : %s" % gh.me().get("login"))
    log("令牌来源  : %s" % tok_src)
    log("镜像      : %s" % ", ".join(images))
    log("平台/架构 : %s (%s)" % (plat["docker_platform"], arch))
    log("中转仓库  : %s/%s @ %s  (%s)" % (owner, repo, ref, repo_src))
    log("工作流    : %s  (模式 %s)" % (workflow, mode))

    if mode == "release":
        log("提示      : Release 单附件上限 2 GiB, 大镜像请改用默认的 artifact 模式")

    ensure_ready(gh, owner, repo, workflow)

    baseline = {r["id"] for r in gh.workflow_runs(owner, repo, workflow, per_page=10)}
    payload = {"docker_images": ",".join(images)}
    if mode == "artifact":
        payload["retention_days"] = str(max(1, min(90, args.retention_days)))
    log("触发工作流 ...")
    gh.dispatch(owner, repo, workflow, ref, payload)

    run = wait_for_run(gh, owner, repo, workflow, baseline, args.timeout, show_progress)
    log("运行结果: %s" % run["conclusion"])
    report_run(gh, owner, repo, run["id"])

    if run["conclusion"] != "success":
        log("工作流未成功, 运行页: %s" % run["html_url"])
        if args.json:
            print(json.dumps({"ok": False, "run_url": run["html_url"],
                              "conclusion": run["conclusion"]}, ensure_ascii=False))
        return 1

    try:
        if mode == "release":
            saved = fetch_release(gh, owner, repo, plat["release_tag"], arch, out_dir,
                                  images, args.connections, show_progress)
        else:
            saved = fetch_artifact(gh, owner, repo, run["id"], arch, out_dir,
                                   images, args.connections, show_progress)
    except (RuntimeError, GitHubError) as e:
        log("下载失败: %s" % e)
        log("运行页 (可手动下载): %s" % run["html_url"])
        if args.json:
            print(json.dumps({"ok": False, "error": str(e), "run_url": run["html_url"]},
                             ensure_ascii=False))
        return 3

    loaded = []
    if args.load:
        if not docker_available():
            log("跳过 docker load: 本机未检测到可用的 docker。")
        else:
            for f in saved:
                log("docker load -i %s" % f)
                proc = subprocess.Popen(["docker", "load", "-i", f],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                spinner = ui.Spinner("导入镜像", enabled=show_progress).start()
                while proc.poll() is None:
                    spinner.update(os.path.basename(f))
                    time.sleep(0.4)
                out, err = proc.communicate()
                ok = proc.returncode == 0
                spinner.stop("导入完成" if ok else "导入失败")
                log("  %s" % (out.strip().splitlines()[-1] if out.strip() else err.strip()[:200]))
                loaded.append({"file": f, "ok": ok})

    print()
    print("=" * 66)
    print("完成: %d 个镜像文件" % len(saved))
    for f in saved:
        print("  %-58s %10s" % (os.path.basename(f), human(os.path.getsize(f))))
    print()
    print("导入方式:  docker load -i <上面的文件名>")
    print("运行页  :  %s" % run["html_url"])

    if args.json:
        print(json.dumps({"ok": True, "run_url": run["html_url"], "platform": args.platform,
                          "images": images, "files": saved,
                          "sizes": {os.path.basename(f): os.path.getsize(f) for f in saved},
                          "loaded": loaded}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
