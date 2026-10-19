#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Install this repository as an agent skill.

Copies the project into a skills directory so an agent runtime (WorkBuddy,
Claude Code, or anything that reads ``SKILL.md``) can load it directly.
The installed copy gets a small ``.repo`` file recording which GitHub repo it
should dispatch, because a copied directory has no ``.git`` to detect from.

Examples:
    python scripts/install_skill.py --target workbuddy
    python scripts/install_skill.py --target claude
    python scripts/install_skill.py --target custom --path ~/my-skills --force
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

SKILL_NAME = "Docker_images_Download"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IGNORE = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".repo", "*.zip")


def preset_path(target: str) -> str | None:
    home = os.path.expanduser("~")
    return {
        "workbuddy": os.path.join(home, ".workbuddy", "skills"),
        "claude": os.path.join(home, ".claude", "skills"),
    }.get(target)


def detect_repo() -> str:
    """Read the origin remote of this checkout, if there is one."""
    try:
        import subprocess

        proc = subprocess.run(["git", "-C", ROOT, "remote", "get-url", "origin"],
                              capture_output=True, text=True, timeout=10)
        url = (proc.stdout or "").strip()
    except Exception:
        return ""
    if not url:
        return ""
    if "://" in url:
        path = "/".join(url.rstrip("/").split("/")[-2:])
    elif "@" in url and ":" in url:
        path = url.split(":", 1)[1]
    else:
        return ""
    path = path[:-4] if path.endswith(".git") else path
    parts = [p for p in path.split("/") if p]
    return "%s/%s" % (parts[-2], parts[-1]) if len(parts) >= 2 else ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Install Docker_images_Download as an agent skill")
    ap.add_argument("--target", default="workbuddy", choices=["workbuddy", "claude", "custom"],
                    help="安装到哪个技能目录 (默认 workbuddy)")
    ap.add_argument("--path", default=None, help="--target custom 时的技能根目录")
    ap.add_argument("--repo", default=None, help="写入 .repo 的中转仓库 owner/name")
    ap.add_argument("--force", action="store_true", help="目标已存在时覆盖")
    args = ap.parse_args()

    base = args.path if args.target == "custom" else preset_path(args.target)
    if not base:
        ap.error("--target custom 需要同时给 --path")
    dest = os.path.join(os.path.abspath(os.path.expanduser(base)), SKILL_NAME)

    if os.path.exists(dest):
        if not args.force:
            print("目标已存在: %s\n加 --force 覆盖，或先手动删除。" % dest)
            return 2
        shutil.rmtree(dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copytree(ROOT, dest, ignore=IGNORE)

    repo = args.repo or detect_repo()
    if repo:
        with open(os.path.join(dest, ".repo"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(repo + "\n")

    print("已安装到: %s" % dest)
    print("中转仓库: %s" % (repo or "（未识别，运行时请用 --repo 指定）"))
    print()
    print("验证: python \"%s\" nginx:alpine --check" % os.path.join(dest, "scripts", "fetch_image.py"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
