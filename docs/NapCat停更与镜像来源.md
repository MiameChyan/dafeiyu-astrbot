# NapCat 停更与镜像来源

> 2026-11-15 记录。适用于本仓库所有部署套件（`deploy/`、`deploy-macos/`、`deploy-windows/`）。

## 发生了什么

2026-11-12，NapCat 官方仓库 `NapNeko/NapCatQQ` 删除了全部代码与 Release，README 只剩一句「到这里告一段落」；`NapNeko/NapCat-Docker` 仓库与 Docker Hub 镜像 `mlikiowa/napcat-docker` 同时下架。

- 已经部署好的实例不受影响：本地镜像和解压好的 Shell 还在，照常运行。
- 新部署拿不到官方产物：`docker compose pull` 会报 `pull access denied`，各套件里钉的 `v4.18.37` 已无法下载。
- NapCat 不会再更新。QQ 改协议或强制升级后它迟早失效，届时需要换协议端（候选与取舍见下文）。

## 现在用什么

社区备份 [XingYunStar/NapCatQQ](https://github.com/XingYunStar/NapCatQQ) 的 **v4.18.42** Release，其中有 `docker save` 导出的镜像包：

| 文件 | 平台 | sha256 |
|---|---|---|
| `napcat-docker-v4.18.42-amd64.tar` | linux/amd64 | `85e3fd02b58fd836a0177734b6ff77a896000bcfcf209b3b200d7e8b8452df7c` |
| `napcat-docker-v4.18.42-arm64.tar` | linux/arm64 | `0d14594844e9135c760e710754a8778d98649de2ca837fce5a61b69b7d39954d`（未做下文的逐文件核验） |
| `NapCat.Shell.zip` | 通用 Shell | `02e5ff0f79916c1e04c81c1b4590056406fb4293f1f1300c955e71fd08b2829d` |

备份只有 v4.15.0 / v4.17.23 / v4.18.19 / v4.18.42 四个 Release，没有原来钉的 4.18.37/38，所以统一改用 4.18.42。

导入：

```bash
sha256sum napcat-docker-v4.18.42-amd64.tar      # 与上表一致再继续
docker load -i napcat-docker-v4.18.42-amd64.tar  # 得到 mlikiowa/napcat-docker:v4.18.42
```

compose 里写 `image: mlikiowa/napcat-docker:v4.18.42` 并加 `pull_policy: never`，避免 compose 再去 Docker Hub 拉。

## 核验结论（amd64，2026-11-15）

镜像会拿到 QQ 小号的登录态，所以用之前做了逐文件核验。官方发布页已删、Internet Archive 也没有 v4.18.42 的发布页存档，无法直接比对官方哈希，改为分层核验：

1. **源码**：备份仓库 v4.18.37 / v4.18.38 / v4.18.42 三个 tag 的提交 SHA，在官方 `NapNeko/NapCatQQ` 仓库网络里都能查到（作者 MliKiowa）。Git 提交 SHA 覆盖整棵源码树，因此这些 tag 的源码与官方一致。
2. **NapCat 包**：镜像里的 `/app/NapCat.Shell.zip` 与 Release 单独发布的那份字节相同。按官方 CI（`build-ci.yml`：Node 20、pnpm 9、`NAPCAT_VERSION=4.18.42`，依次构建 webui-frontend、shell、plugin-builtin，再 `npm pkg delete devDependencies && npm install --omit=dev`）用 v4.18.42 源码在 `node:20-bookworm` 容器里重建：
   - 构建产物 141 个文件中 140 个字节相同（含 3.3MB 的 `napcat.mjs`、Windows 原生模块、WebUI 静态文件）；`package.json` 在执行 CI 的删 devDependencies 步骤后也相同。
   - 运行时依赖 543 个文件：重装后只有 `ws`、`encodeurl` 不同，原因是两者在 2026-11-12 发布了新版。zip 里的 `ws@8.22.0`、`encodeurl@2.0.1` 与 npm 官方 tarball 逐文件相同。
   - 我方多出的 61 个点文件（`.eslintrc`、`.github/` 等）是 GitHub Actions 上传产物时默认排除隐藏文件所致。
3. **QQ**：镜像构建命令从腾讯官方 `qqdl.gtimg.cn` 下载 `linuxqq_3.2.34-53644_amd64.deb`。同一地址下载的官方包（sha256 `436c65e1dd28422e128a21cbddb5fe4e2e6c079d7f27fec2541e014d38cc336e`）里的 1536 个文件，在镜像 QQ 层中全部存在且字节相同。镜像层多出的 834 个文件是安装副产物：官方 `postinst` 调用 `update-mime-database` 生成的 `usr/share/mime/*`、dpkg 记录与日志、一个图标缓存。
4. **启动脚本** `entrypoint.sh`：通读，没有任何网络请求或上传。职责是首次解压 NapCat、按 `MODE` 写反向 WS 配置（`astrbot` 模板指向 `ws://astrbot:6199/ws`，`reportSelfMessage: false`）、伪装容器特征（hostname、cgroup、DMI 等，用来降低 QQ 风控）、用 Xvfb 启动 QQ。
5. **镜像元数据**：标签 `mlikiowa/napcat-docker:v4.18.42`，构建于 2026-10-30 07:08（v4.18.42 提交后 9 分钟），各层构建命令与 NapCat-Docker 官方流水线一致。

结论：amd64 镜像中的 NapCat 与 QQ 均为原版。arm64 包没有做同样的核验，macOS 上使用前应照此流程再核一遍。

## 以后换协议端的候选

- **LuckyLilliaBot（LLBot，原 LLOneBot）**：在维护，支持 OneBot 11 反向 WS，官方文档有 AstrBot 对接页。但无头与有头（PMHQ，闭源）两种模式都必须使用 `auth.luckylillia.com` 发放的 Auth Token：需 GitHub 账号（新账号要人工审核），QQ 号登记进对方白名单，可被集中封禁；其管理后台界面提到保留 sign/消息记录、AI 审查消息、每 30 分钟上报主机进程名。隐私与依赖风险需要搭档自行权衡。
- **Lagrange**：开源纯协议实现，仍在维护；签名服务情况未调研。
- 换协议端后要同步改：各套件 compose、`deploy/heal_ws.py`（依赖 NapCat WebUI API）、踩坑合集中的 NapCat 专属条目、记忆与 TRPG 规格里写「NapCat」的地方（应改为「OneBot v11 协议端」）。
