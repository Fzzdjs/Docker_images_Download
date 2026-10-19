# 排查手册

## 第 0 步：先体检

```bash
python scripts/fetch_image.py --check
```

它会打印账号、仓库可见性、默认分支、push 权限、Actions 是否启用、工作流清单，
并把缺失项直接标出来。绝大多数"跑不起来"在这里就能定位。

```
账号      : your-name
仓库      : your-name/Docker_images_Download  (public)
默认分支  : main
push 权限 : 有
Actions   : 已启用
工作流    : amd64-release.yml, amd64.yml, arm32-release.yml, ...

  ✓ 配置可用，可以正常触发。
```

## 一、令牌与权限

| 症状 | 原因 | 处理 |
|---|---|---|
| `No GitHub token found` | 三种来源都没有 | `gh auth login`，或 `export GITHUB_TOKEN=...` |
| `403 Resource not accessible by integration` | 令牌只读，或缺 Actions 写权限 | 换有 `workflow`（经典）/ Actions:write（细粒度）的令牌 |
| `403` 但令牌看起来有权限 | 用了 GitHub App 安装令牌，权限被限制 | 改用个人 PAT |
| `令牌对 xxx 没有写权限` | 指向了上游仓库，或不是仓库所有者 | fork 后 `--repo 你的账号/Docker_images_Download` |
| `404 Not Found` 访问仓库 | 仓库私有但令牌无权，或仓库名拼错 | 确认 `owner/name` 与令牌归属 |
| 网页能操作，脚本 403 | 经典令牌缺少 `workflow` scope | 重新生成并勾选 `workflow` |

> 权限类错误不要靠"重新登录"解决：登录态没问题，问题在 scope。

## 二、工作流侧

| 症状 | 原因 | 处理 |
|---|---|---|
| `仓库里没有 amd64.yml` | fork 时没带上 `.github/`，或分支不对 | 确认 `--ref`；重新 fork 或从上游复制工作流 |
| `未启用 Actions` | fork 默认可能关着 | Settings → Actions → 允许 |
| 卡在 `queued` 很久 | 免费账号并发额度用满 | 等同仓库其他运行结束；或换账号/自建 runner |
| 失败在 `Pull images and package` | 镜像名错、tag 不存在、没有该平台变体 | 打开运行页看 `::error::` 那一行 |
| `manifest unknown` | tag 拼错或已被删除 | `docker manifest inspect` 核对 |
| `no matching manifest for linux/arm/v7` | 该镜像没有 32 位 ARM 变体 | 换 `--platform amd64` 或换镜像 |
| `No space left on device` | 镜像过大，运行器磁盘不足 | 换更小的 tag；或自建 runner（磁盘更大） |
| 运行被取消 | 超过 `timeout-minutes: 90` | 拆分镜像，或调大超时 |

## 三、下载侧

| 症状 | 原因 | 处理 |
|---|---|---|
| 单连接只有 30–80 KB/s | Azure Blob 对国内单连接限速 | 保持默认 `--connections 16`；实测 16 路约 0.7 MB/s |
| 下载中 HTTP 403 | 签名 URL 有 TTL，长下载会过期 | 脚本会自动换新地址重试；持续失败则重跑 |
| `401 InvalidAuthenticationInfo` | 令牌被带到了 Blob（自写脚本常见） | 认证头必须用 `add_unredirected_header` |
| `该运行没有名为 docker-images-xxx 的产物` | 工作流没跑到上传步骤，或架构不匹配 | 看运行页；确认 `--platform` |
| `产物已过期` | 超过 Artifact 保留期 | 重跑，或用 `--retention-days 7` |
| 磁盘空间告警 | zip + 解出的 tar.gz 需要约 2.2 倍体积 | 换到空间更大的 `--out` |
| 下载完成后文件明显偏小 | 分片失败被忽略（自写脚本常见） | 校验 gzip CRC 与 `manifest.json` |

## 四、Release 模式

| 症状 | 原因 | 处理 |
|---|---|---|
| `Release xxx 下没有 *-amd64.tar.gz 附件` | 工作流没成功，或 tag 不匹配 | 打开 Release 页确认 |
| 失败在 `Check release asset size limit` | 单附件超过 2 GiB | 改用默认 `auto` 模式 |
| 下载到了不相关的镜像 | 固定 tag 下历史附件会累积 | 脚本按镜像名筛选；多余附件去 Release 页删掉 |
| 链接给别人打不开 | Release 在私有仓库 | 仓库必须公开，或让对方用 `auto` 模式 |

## 五、验证下载结果

```bash
# 1) gzip 完整性
python -c "import gzip;gzip.open('xxx.tar.gz','rb').read(1);print('gzip ok')"

# 2) 包内镜像名
python -c "import tarfile,json;t=tarfile.open('xxx.tar.gz');print(json.load(t.extractfile('manifest.json'))[0]['RepoTags'])"

# 3) 导入后核对
docker load -i xxx.tar.gz
docker images | head
```

正常输出示例：`['nginx:alpine']`。

## 六、手动触发（不依赖脚本）

怀疑脚本有问题时，直接用 curl 排除：

```bash
OWNER=your-name
REPO=Docker_images_Download

# 触发
curl -X POST \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/$OWNER/$REPO/actions/workflows/amd64.yml/dispatches \
  -d '{"ref":"main","inputs":{"docker_images":"nginx:alpine","retention_days":"1"}}'

# 查最近运行
curl -s -H "Authorization: Bearer $GITHUB_TOKEN" \
  "https://api.github.com/repos/$OWNER/$REPO/actions/workflows/amd64.yml/runs?per_page=3" \
  | grep -E '"id"|"status"|"conclusion"'
```

输入与命名规则：

- 触发接口：`POST /repos/{owner}/{repo}/actions/workflows/{file}/dispatches`
- body：`{"ref":"main","inputs":{"docker_images":"a:b,c:d","retention_days":"1"}}`
  （`retention_days` 仅产物版接受）
- 镜像名转换：`/`、`:`、`@` → `_`
  例：`registry.k8s.io/pause:3.10` → `registry.k8s.io_pause_3.10-amd64.tar.gz`
- 产物名：`docker-images-amd64` / `-arm64` / `-arm32`
- Release tag：`DockerImages-AMD64` / `-ARM64` / `-ARM32`

## 七、方案对照

| 方案 | 原理 | 适用 | 边界 |
|---|---|---|---|
| `registry-mirrors` | Docker 走第三方镜像站 | 只要 docker.io 的常见镜像 | 站点随时关停；不覆盖其他 registry |
| 公共代理 | HTTP 反代 | 临时应急 | 可用性波动，内容难验证 |
| **本项目** | 海外运行器代拉 + 导出 | 任意公开 registry、离线包、分享 | 依赖 `api.github.com`；单次 1–3 分钟 |
| Harbor proxy cache | 自建缓存 | 团队长期高频 | 需服务器、域名、证书、运维 |
| 云厂商 CR 同步 | 云端同步公共镜像 | 生产环境 | 需云账号；同步规则受限 |

## 八、仍然失败时请提供

1. `python scripts/fetch_image.py --check` 的完整输出
2. 执行的完整命令与全部控制台输出
3. 运行页 URL（如果有）
4. 环境：操作系统、Python 版本、`docker version` 输出
5. 镜像名与 `--platform`

有了这五项，绝大多数问题可以一次定位。
