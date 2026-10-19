---
name: Docker_images_Download
description: 借 GitHub Actions 中转，把国内拉不动或拉取极慢的 Docker 镜像导出为 tar.gz 并下载回本地导入。当用户在国内网络下遇到 docker pull 超时/失败、没有可用的镜像加速器或代理、需要 k8s 组件镜像（registry.k8s.io）、需要 ghcr.io / quay.io / mcr.microsoft.com 镜像、需要离线分发或在无外网机器上导入镜像、需要把镜像打成 tar 包分享给别人时使用。支持 amd64 / arm64 / arm32，支持导出到 Actions 产物或公开 Release 链接。
agent_created: true
---

# Docker_images_Download

在国内网络环境下，`docker pull` 对 `docker.io` 经常超时或被重置，`registry.k8s.io`、
`ghcr.io`、`quay.io`、`mcr.microsoft.com` 更是基本不可达，公共镜像加速器则随时可能失效。

本技能的做法：**借用 GitHub 的海外运行器代拉**。在你的中转仓库里触发一次 GitHub Actions
工作流，运行器执行 `docker pull` + `docker save | gzip`，脚本再把产物以多线程分片方式
下载回本地。运行器能访问任何公开 registry，因此不限于 Docker Hub。

仓库自包含：6 个工作流就在 `.github/workflows/` 里，fork 后即可用，不依赖任何外部中转仓库。

## 何时使用

- "拉不下来 / pull 超时 / 没有加速器 / 镜像下载不动"
- 需要 `registry.k8s.io/pause`、`kube-apiserver` 等 k8s 组件镜像
- 需要把镜像带到**离线 / 内网机器**（先导出 tar.gz，再 `docker load`）
- 需要把一个镜像**分享给别人**（`--mode release` 生成公开下载链接）
- 镜像站不可用，或不想依赖第三方镜像站

不适用：私有 registry 且 GitHub 运行器无法凭据访问的镜像。运行器只能拉公开镜像；
私有镜像需要在工作流里配置 secrets 并自行改造。

## 前置条件

1. 一个 GitHub 账号，并 **fork 本仓库**（或把工作流复制到自己的仓库）。
2. 一个具备 **Actions 写权限** 的令牌：
   - 装了 GitHub CLI：`gh auth login` 即可，脚本会自动读取 `gh auth token`
   - 或设置环境变量 `GITHUB_TOKEN`
   - 经典令牌权限：`public_repo` + `workflow`；细粒度令牌：Contents=Read、Actions=Read and write
3. 本机能访问 `api.github.com`。

## 快速使用

脚本仅依赖 Python 标准库（3.9+），无需安装任何依赖。

```bash
# 1) 最常用：拉一个镜像到当前目录
python scripts/fetch_image.py nginx:alpine

# 2) 指定输出目录，并直接导入本地 docker
python scripts/fetch_image.py nginx:alpine --out ./images --load

# 3) 拉 ARM64 镜像（树莓派 / ARM NAS）
python scripts/fetch_image.py alpine:latest --platform arm64

# 4) 国内基本不可达的 k8s registry
python scripts/fetch_image.py registry.k8s.io/pause:3.10

# 5) release 模式：发布到 Release，得到可分享的公开链接
python scripts/fetch_image.py my/app:v1 --mode release

# 6) 只检查令牌 / 仓库 / 工作流是否就绪，不触发运行
python scripts/fetch_image.py --check

# 7) 机器可读输出（自动关闭进度条）
python scripts/fetch_image.py nginx:alpine --json

# 8) 重定向到日志时关闭进度条
python scripts/fetch_image.py nginx:alpine --no-progress > fetch.log 2>&1
```

拿到文件后导入：

```bash
docker load -i xxx.tar.gz     # Docker 能自动识别 gzip，无需先解压
```

## 进度显示

```
[13:30:02] 触发工作流 ...
等待工作流 工作流运行中 / 00:47  执行中  (上限还剩 29:13)
下载产物 [███████████████░░░░░░░░░░░░░░░░░░░░░░░]  46.2%  472.6 MB / 1023.1 MB  1.32 MB/s  剩余 06:58
解包 [██████████████████████████████] 100.0%  1023.1 MB / 1023.1 MB
```

- 工作流等待：旋转指示器 + 已用时间 + 状态 + 剩余上限。
- 下载：百分比、已完成/总量、瞬时速度（最近几秒滑动窗口）、ETA；终端窄时自动省略 ETA。
- 解包：按解压字节数推进，大镜像不会"黑屏几分钟"。
- 非 TTY 自动降级：重定向到文件/管道时每 10% 打一行，不刷 `\r`。

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `images` | 必填（`--check` 除外） | 一个或多个镜像名，空格分隔 |
| `--platform` | `amd64` | `amd64` / `arm64` / `arm32`，决定用哪个工作流 |
| `--mode` | `auto` | `auto` 走 Actions 产物（限额大）；`release` 发到 Release 生成公开链接 |
| `--repo` | 自动识别 | 中转仓库 `owner/name`。默认取当前 checkout 的 git remote，其次 `.repo` 配置，最后上游仓库 |
| `--ref` | 当前分支 → `main` | 触发所用的分支 |
| `--out` | `.` | tar.gz 保存目录 |
| `--load` | 关 | 下载后自动 `docker load` |
| `--timeout` | `1800` | 等待工作流完成的秒数上限 |
| `--retention-days` | `1` | 产物保留天数（1–90），仅产物模式 |
| `--connections` | `16` | 分片并发数；国内单连接常只有几十 KB/s，并发后可到 0.7–1.5 MB/s |
| `--no-progress` | 关 | 关闭进度显示 |
| `--check` | 关 | 只体检，不触发运行 |
| `--json` | 关 | 输出 JSON 摘要 |
| `--version` | — | 打印版本 |

## 两种模式怎么选

| | auto（Actions 产物） | release（Release 附件） |
|---|---|---|
| 适用 | 默认，即取即用 | 需要**公开可分享**的固定链接 |
| 容量 | 受运行器磁盘限制（约 10–14 GB） | **单附件 ≤ 2 GiB**，超限工作流直接失败 |
| 认证 | 下载需要令牌 | 附件公开，凭链接即可下载 |
| 保留 | 默认 1 天（1–90 可调） | 长期保留；同名覆盖，不同镜像会累积 |

大于 2 GiB 的镜像必须用默认的 auto 模式。

## 执行要点

1. 工作流通常 **1–3 分钟**结束；1 GB 镜像约 4–5 分钟。
2. 下载走 16 路 Range 并发，产物/附件落在 Azure Blob，国内单连接被限速到 30–80 KB/s，
   并发后聚合约 0.7 MB/s。`--connections` 可调，但超过出口带宽后不再线性增长。
3. 下载前会检查目标目录空间：zip + 解包约需 **2.2 倍**镜像体积。
4. 工作流失败时会打印运行页地址，可在浏览器看日志并手动下载产物。
5. 镜像 > 5 GB 时提醒用户：托管运行器磁盘可能不足，工作流会明确报错并打印 `df -h`。

## 常见问题定位

详见 `docs/troubleshooting.md`。最高频的三类：

| 症状 | 原因 | 处理 |
|---|---|---|
| 启动即报"没有 GitHub 令牌" | 未登录 gh CLI、未设环境变量 | `gh auth login` 或 `export GITHUB_TOKEN=...` |
| `403 Resource not accessible` | 令牌只读，或缺 Actions 写权限 | 换有 `workflow` / Actions:write 的令牌 |
| `令牌对 xxx 没有写权限` | 默认指向了上游仓库 | 先 fork，再 `--repo 你的账号/Docker_images_Download` |

## 作为技能安装

```bash
python scripts/install_skill.py --target workbuddy    # ~/.workbuddy/skills/Docker_images_Download
python scripts/install_skill.py --target claude       # ~/.claude/skills/Docker_images_Download
python scripts/install_skill.py --target custom --path ~/skills --force
```

安装副本没有 `.git`，脚本会在目录里写入 `.repo` 记住中转仓库；也可在运行时用 `--repo` 覆盖。
