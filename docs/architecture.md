# 架构与设计取舍

## 组件

```
┌──────────────┐        ┌──────────────────┐        ┌────────────────────┐
│ fetch_image  │        │  GitHub REST API │        │ Actions Runner     │
│   (CLI)      │───────▶│  workflow_dispatch│──────▶│ ubuntu-24.04       │
│              │        │  状态轮询 / Artifact │        │ docker pull        │
│ gh_client    │◀───────│   Release        │◀───────│ docker save | gzip │
│ parallel_dl  │        └──────────────────┘        └────────────────────┘
│ progress     │                 │ 302
└──────┬───────┘                 ▼
       │              ┌──────────────────────┐
       └─────────────▶│ Azure Blob (签名 URL) │  16 路 Range 并发
                      └──────────────────────┘
```

| 文件 | 职责 | 关键约束 |
|---|---|---|
| `fetch_image.py` | 参数解析、仓库识别、触发、轮询、编排、`--load` | 只依赖标准库 |
| `gh_client.py` | REST 调用、令牌解析、签名 URL 交换 | 令牌不跟随 302 |
| `parallel_dl.py` | Range 探测、分片调度、重试、进度上报 | 单片 8 MB，256 KiB 计账 |
| `progress.py` | 进度条 / 旋转指示器 | TTY 自适应，非 TTY 降级 |
| `.github/workflows/*.yml` | 在运行器上拉取并打包 | 最小权限 + 超时保护 |

## 为什么用 GitHub Actions，而不是自建反代

| 维度 | Actions 中转 | 自建 registry 反代 |
|---|---|---|
| 初始成本 | 0（用现成账号） | 一台境外机器 + 域名 + 证书 |
| 覆盖 registry | 任意公开 registry | 取决于配置，通常要逐个加 |
| 带宽 | GitHub 出口，到各大 registry 都通 | 取决于机器线路 |
| 运维 | 无 | 磁盘、证书、升级、限流 |
| 高频批量 | 不合适（单次 1–3 分钟） | 合适 |
| 依赖 | `api.github.com` 可达 | 自己的机器可用 |

结论：**偶发、跨 registry、需要离线包** → Actions 中转；**团队日常高频** → 自建缓存。
两者可以并存，本项目的 `--repo` 也允许把工作流跑在自建 runner 上。

## 为什么默认用 Artifact 而不是 Release

- Artifact 上限由运行器磁盘决定（约 10–14 GB 实存），Release 单附件硬上限 **2 GiB**。
- Artifact 下载需要令牌，天然私有；Release 附件是公开的，只有需要分享时才用。
- Artifact 有保留期（默认 1 天），避免仓库里堆积几 GB 的旧包。

`--mode release` 的定位是"给别人一个链接"，不是"长期存储"。

## 为什么必须并发分片

Azure Blob / Fastly 对来自国内的**单条连接**限速明显。实测（1 GB 镜像）：

| 并发 | 平均速度 | 1 GB 用时 |
|---|---|---|
| 1 | 32 KB/s | ~9 小时 |
| 8 | ~0.55 MB/s | ~31 分钟 |
| 16 | ~0.70 MB/s | ~24 分钟 |
| 32 | ~0.72 MB/s | ~24 分钟 |

16 路之后基本到出口带宽上限，继续加大只会增加重试与内存压力。

单片默认 8 MB 是折中：太小则请求数暴增（TLS 握手与首字节延迟摊不薄），
太大则尾部负载不均、单点重试代价高。

不过分片大小会按文件自适应：`chunk = clamp(total / connections, 1 MB, 8 MB)`。
实测一个 3.5 MB 的 Release 附件：单连接约 11 KB/s，耗时 5 分 15 秒；
切成 4 个 1 MB 分片并发后约 53 KB/s，耗时 1 分 10 秒（约 5 倍）。
限速是按连接生效的，小文件同样值得拆。

## 为什么令牌不能跟随 302

```
GET /repos/{o}/{r}/actions/artifacts/{id}/zip
Authorization: Bearer ghp_xxx
        │
        └──▶ 302 Location: https://productionresultssa*.blob.core.windows.net/...?sig=...
```

urllib 默认会把原请求头带到跳转目标。Blob 只认 `sig` 查询参数，看到
`Authorization` 反而返回 `401 InvalidAuthenticationInfo`。所以：

1. 用 `HTTPRedirectHandler` 子类拦截 302，拿到 `Location`；
2. 令牌用 `add_unredirected_header` 添加，跨主机时不发送；
3. 之后所有分片请求都是**裸请求**，只靠 URL 里的签名。

签名有 TTL。长下载遇到 403 时，`parallel_dl` 调用 `refresh` 回调重新换一次地址，
已下载的分片不会重来。

## 仓库识别顺序

`--repo` 未显式给出时：

1. 环境变量 `DID_REPO`
2. 安装副本里的 `.repo` 文件（`install_skill.py` 写入）
3. 当前 checkout 的 `git remote origin`（仅当目录里有 `.github/workflows/amd64.yml`）
4. 上游 `Fzzdjs/Docker_images_Download`

第 3 条是关键：绝大多数人从自己的 fork 运行，而令牌只对 fork 有写权限。

`--ref` 同理：显式参数 → `DID_REF` → 当前分支 → `main`。

## 容量与配额

| 项目 | 数值 | 说明 |
|---|---|---|
| 运行器磁盘 | ~14 GB 可用 | 工作流先删除 dotnet/Android/CodeQL 等预装目录 |
| 单镜像实际上限 | 约 10 GB（压缩后） | 超过会 `No space left on device` |
| Artifact 保留 | 1–90 天 | 默认 1 天 |
| Release 附件 | ≤ 2 GiB | GitHub 硬限制 |
| 免费账号并发 | 有限 | 同仓库运行串行（`concurrency` 已设） |
| 单次运行耗时 | 1–5 分钟 | 取决于镜像大小与排队 |

## 自建运行器

在仓库 `Settings → Secrets and variables → Actions → Variables` 增加：

```
RUNNER_LABELS = ["self-hosted","linux","x64"]
```

工作流的 `runs-on` 是 `fromJSON(vars.RUNNER_LABELS || '["ubuntu-24.04"]')`，
设置后自动切到自建机，删除变量即回托管机，**无需改代码**。

自建机的收益是下载环节可以走更好的线路，且不受 GitHub 免费额度限制；
代价是要自己维护磁盘清理与安全。配置方案（含选型、成本、加固、回退）见
[DockerTarBuilder/docs/alibaba-cloud-swas-fallback.md](https://github.com/Fzzdjs/DockerTarBuilder/blob/master/docs/alibaba-cloud-swas-fallback.md)。

## 错误模型

| 退出码 | 含义 | 典型原因 |
|---|---|---|
| 0 | 成功 | — |
| 1 | 工作流失败 | 镜像不存在、平台不匹配、磁盘不足 |
| 2 | 参数或凭据问题 | 无令牌、只读令牌、仓库无写权限、Actions 未启用 |
| 3 | 下载失败 | 产物过期、分片重试耗尽、Release 无匹配附件 |

工作流失败时脚本会打印运行页地址与逐步骤结果，便于直接定位到失败的那一步。
