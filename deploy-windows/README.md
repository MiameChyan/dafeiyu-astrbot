# 🪟 Windows 本机部署套件（Docker Desktop）

> 在 Windows 上用两个容器跑起伽拉忒亚：AstrBot 是大脑，NapCat 是 QQ 协议端。
> 写法照搬 [`deploy-macos/`](../deploy-macos/README.md)：宿主路径都是相对路径，端口 / 时区 / QQ 小号走 `.env`，
> 镜像构建复用 `../deploy/Dockerfile`，核心补丁复用 `../deploy/patches/`。

## 和 macOS 套件的区别

- **NapCat 镜像来自社区备份**：官方已停更下架，`compose.yaml` 用 `mlikiowa/napcat-docker:v4.18.42` 加
  `pull_policy: never`，启动前要先 `docker load` 导入镜像包。来源与核验见
  [NapCat 停更与镜像来源](../docs/NapCat停更与镜像来源.md)。
- **多映射一个海豹 WebUI 端口**（默认 `127.0.0.1:3211`）：海豹由骰子插件 `astrbot_plugin_trpg_dice` 在
  AstrBot 容器里以子进程托管（TRPG 规格 §9.1），首次要在海豹 WebUI 里添加指向骰子插件端点的 OneBot 账号。
  骰子端点 18779 只在容器内部使用，不映射。
- **TRPG 本地导入目录**就是 `data/plugin_data/trpg/modules`（`modules_dir` 默认值），在 Windows 上是个普通文件夹。
  海豹程序放在 `data/sealdice/`，`sealdice_path` 填容器内路径 `/AstrBot/data/sealdice/sealdice-core`（Linux 版）。
- 启动脚本是 PowerShell：`start.ps1` / `stop.ps1`。

## 快速开始

前置：Docker Desktop（WSL2 后端，给 4GB 以上内存）、一个 **QQ 小号**。

```powershell
docker load -i napcat-docker-v4.18.42-amd64.tar   # 先核对 sha256，见上面的文档
powershell -ExecutionPolicy Bypass -File deploy-windows\start.ps1
```

`start.ps1`：检查 docker → 建 `data/` `napcat/` → 首次从 `.env.example` 生成 `.env` → 构建 AstrBot 镜像 → 起容器 → 等面板就绪。

### 1. 登录 QQ

打开 `http://127.0.0.1:6099/webui`。token 在 `napcat\config\webui.json` 的 `token` 字段（或启动日志里的
`WebUi Token`）。扫码登录小号；指向 AstrBot 的反向 WS（`ws://astrbot:6199/ws`）已由 `MODE=astrbot` 自动写好。

### 2. 配 AstrBot

打开 `http://127.0.0.1:6185`。初始账号密码只打印在日志里，登录后立刻改密码：

```powershell
docker compose logs astrbot | Select-String "Initial"
```

- **消息平台**：新增 QQ 个人号（aiocqhttp），反向 WS 主机 `0.0.0.0`、端口 `6199`。
- **服务提供商**：建 DeepSeek 源（`https://api.deepseek.com/v1` + key）后，**还要在源下添加模型**（`deepseek-flash`
  或 `deepseek-v4-pro`）并设为默认，否则会报「未找到任何可用的对话模型」。
- **配置**：`platform_settings.ignore_bot_self_message` 设为 `true`；唤醒词 `wake_prefix` 加上「大肥鱼」。

### 3. 人格与插件

- 人格：新建人格 `伽拉忒亚`，系统提示词为 [伽拉忒亚人格提示词](../persona/伽拉忒亚-人格提示词.md) 中 `---` 之后的正文，设为默认
  （v4.29.0 读的是 `agent_runner.config.persona.persona_id`）。
- 插件：`python deploy-kit/preflight.py --stage-payload <目录>` 组装干净插件，把 `plugins/` 下的文件夹拷进 `data\plugins\`，
  重启 AstrBot。依赖（dashscope、imageio-ffmpeg、aiohttp）镜像里已有。

## 日常运维

```powershell
.\start.ps1 -NoBuild               # 日常重启，不重建镜像
.\stop.ps1                         # 停容器，数据保留
.\stop.ps1 -Down                   # 删容器与网络，数据保留
docker compose restart napcat      # 只重启 NapCat（别用 up -d 重建：设备指纹会变，容易触发风控）
docker compose logs -f astrbot     # 跟日志
```

AstrBot 重启后 NapCat 会在 30 秒内自动重连（2026-11-15 实测）。连不回来时用 `python ..\deploy\heal_ws.py` 自愈。

## 验证状态（2026-11-15，Windows 11 + Docker Desktop 28.0.1）

- ✅ AstrBot v4.29.0 构建与启动，四个核心补丁挂载到位
- ✅ NapCat v4.18.42 导入、扫码登录小号、反向 WS 连通（6199 ESTABLISHED）
- ✅ 私聊 `/help` 往返、DeepSeek 对话
- ✅ 伽拉忒亚人格、`face_convert`、`schedule_awareness` 加载；AstrBot 重启后 WS 自动重连
- ⏳ 海豹（等骰子插件）、核心补丁 P-T1 / P-M1、记忆插件
