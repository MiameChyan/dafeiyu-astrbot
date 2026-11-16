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

### 4. 活人感相关配置（2026-11-16 实测）

- **分段回复**：`platform_settings.segmented_reply` 设 `enable: true`、`max_segments: 3`，
  `regex` 改成 `.*?[。？！?!~…\n]+|.+$`。人格 v0.3 句末大多不打标点、靠换行分条，默认正则只按句号切，
  会把两行话塞进同一个气泡。`rate_limit.count` 降到 `10`。
- **连发合并 / 表情包不回**：把仓库里的 `plugin/astrbot_plugin_chat_rhythm/` 拷进 `data\plugins\`，然后重启 AstrBot。
  它不在 deploy-kit 的默认插件清单里，要手动装。私聊里对方连发几条时，等 4 秒静默、并且对方不在打字（用 NapCat 的
  「输入状态」通知判断），再一次回复；一直在打字最多等 15 秒。这一批全是表情包（不含普通照片）时，有 60% 的概率不回；连发几张时每多一张减半（2 张 30%，3 张 15%）。没回的表情包会在下一条消息前提一句，让她知道自己看到了。
  以 `/`、`.`、`。` 开头的指令和群聊不受影响。参数在插件配置里改。
- **表情包**：插件市场装「表情包小偷」（`nagatoquin33/astrbot_plugin_stealer`，审查过的是 v3.2.2 / `6a993b2`）。
  开 `steal_meme`；`vision_provider_id` 填对话模型（deepseek-flash 能看图）；保持 `audit_required`（偷来的图先在
  插件的「表情管理」里人工通过才入库）。JEV / TypeSafe 留空不开，否则对话片段会发到第三方。
- **热梗词典**：知识库 + [热梗词典](../docs/知识库-热梗词典示例.md)。
  - 嵌入和重排必须加成**嵌入 / 重排类型**的提供商。挂在对话源下面的模型会被当成聊天模型，知识库选不到。
    面板入口找不到时，可以按 AstrBot 自带模板直接写进 `cmd_config.json` 的 `provider`（AstrBot 停止时改），再到面板里填 key：
    - 嵌入：`type: openai_embedding`，`embedding_api_base: https://api.siliconflow.cn/v1`，`embedding_model: BAAI/bge-m3`，维度 1024；
    - 重排：`type: vllm_rerank`，`rerank_api_base: https://api.siliconflow.cn`（**不带** `/v1`），`rerank_model: BAAI/bge-reranker-v2-m3`；
    - 不想把聊天内容发给第三方时，可改用本机 Ollama：`ollama pull bge-m3`，嵌入类型选 Ollama Embedding，地址 `http://host.docker.internal:11434`。
  - 新建知识库 `热梗词典`，导入词典文档，`kb_names: ["热梗词典"]`。
  - 设 `kb_agentic_mode: true`（按需检索）。默认模式不设分数门槛，每条消息都会把 5 块词典塞进上下文；
    按需模式下，模型遇到不认识的说法才调 `astr_kb_search`。人格提示词里的「梗」规则负责提醒她去查。

### 5. 改用宿主机上的 OpenQQ（可选，替代 NapCat）

NapCat 小号接连被判「身份失效」后，2026-11-16 改用 [OpenQQ](https://github.com/feijx3/OpenQQ)（审查过上游 `14a7bac`）。
实际用的是我们的 fork [MiameChyan/OpenQQ](https://github.com/MiameChyan/OpenQQ) 的 `dafeiyu` 分支（锁定在 `85f4ef0`），
在上游基础上加了「收到图片时让 QQ 下载原图，并以 base64 内嵌」（否则容器里的 AstrBot 看不到图片）、「私聊消息补上发送者昵称」和「实现 `get_msg`」（AstrBot 解析引用回复要用）。
它在 **Windows 桌面版 QQ NT** 里挂接会话，和 NapCat / SnowLuma 同属「挂接官方客户端」，只是跑在真机桌面 QQ 上。
代码全部开源、无预编译二进制、除本机外不连任何地址。

- **获取**：`git clone -b dafeiyu https://github.com/MiameChyan/OpenQQ.git`，然后 `git checkout 85f4ef0`。
- **放在纯英文路径**（如 `D:\RAG-ai-code\OpenQQ`）：cmake 4.2 在含中文的路径上直接崩溃（`0xC0000409`）。
- **构建**：`pip install ninja`（装了 g++ 时它强制用 Ninja）→ `npm ci` → `npm run build`。
- **配置** `config/openqq.json`（不进版本控制）：`qqRoot` 填 QQ 安装目录；只开反向 WS
  `ws://127.0.0.1:6199/ws`，正向 WS 关掉。AstrBot 这边 compose 已把 6199 映射到 `127.0.0.1`。
- **启动顺序**：先停掉 NapCat 容器 → **退出所有 QQ** → `node src/main.mjs start` → 在它拉起的 QQ 里扫码登录小号 →
  需要的话再另开 QQ 登录主号（另开的是独立进程，实测没有加载 OpenQQ 的 DLL）。重启 OpenQQ 前两个 QQ 都要退出。
- **图片**：fork 里收到图片时先让 QQ 下载原图（最多等 10 秒），5 MB 以内以 `base64://` 交给 AstrBot。
  实测 162 KB 的照片与消息同一秒到达，模型能看到图片；超时或超限时退回补全的下载地址（NT 新版图片多半 404）。
- **昵称**：上游私聊事件不带昵称；fork 从好友列表或 QQ 资料服务补上（缓存）。
- **引用回复**：上游没有 `get_msg`，AstrBot 看不到被引用的那句；fork 用本次运行收发过的消息（最多 2000 条）回答，重启后旧消息查不到。
- **已知差异**：掉线监控 `watchdog.py` 目前只认 NapCat。

## 日常运维

```powershell
.\start.ps1 -NoBuild               # 日常重启，不重建镜像
.\stop.ps1                         # 停容器，数据保留
.\stop.ps1 -Down                   # 删容器与网络，数据保留
docker compose restart napcat      # 只重启 NapCat（别用 up -d 重建：设备指纹会变，容易触发风控）
docker compose logs -f astrbot     # 跟日志
```

AstrBot 重启后 NapCat 会在 30 秒内自动重连（2026-11-15 实测）。连不回来时用 `python ..\deploy\heal_ws.py` 自愈。

### 掉线 / 假在线监控

小号被风控时，有时 NapCat 自己还显示「已登录」，但消息已经收不到了（假在线）。`start.ps1` 会在后台用 `pythonw` 拉起 [watchdog.py](watchdog.py)，`stop.ps1` 会先停掉它。它每 10 分钟检查一次：

- **掉线**（60 秒后复查仍失败才算）：NapCat WebUI 连不上；显示未登录或已离线；OneBot `get_status` 离线；`nc_get_rkey` 向 QQ 服务器取数据失败。
- **疑似假在线**：白天（默认北京时间 9–24 点）连续 3 小时一条消息都没收到。NapCat 刚重启不满 3 小时不判。

状态变化时弹 Windows 通知，没恢复就每 2 小时提醒一次，恢复后再通知一次。**只通知，不重启任何东西**：身份失效时快速登录必然失败，反复重启只会更像机器人。日志在 `logs\watchdog.log`，QQ 号、群号等长数字都会打码。

```powershell
python watchdog.py --once          # 手动查一次
python watchdog.py --test-notify   # 试一下通知能不能弹出
.\start.ps1 -NoBuild -NoWatchdog   # 不要监控
```

阈值在 `.env` 里改：`WATCHDOG_INTERVAL_MIN`、`WATCHDOG_SILENCE_HOURS`、`WATCHDOG_ACTIVE_HOURS`。

## 验证状态（2026-11-15，Windows 11 + Docker Desktop 28.0.1）

- ✅ AstrBot v4.29.0 构建与启动，四个核心补丁挂载到位
- ✅ NapCat v4.18.42 导入、扫码登录小号、反向 WS 连通（6199 ESTABLISHED）
- ✅ 私聊 `/help` 往返、DeepSeek 对话
- ✅ 伽拉忒亚人格、`face_convert`、`schedule_awareness` 加载；AstrBot 重启后 WS 自动重连
- ⏳ 海豹（等骰子插件）、核心补丁 P-T1 / P-M1、记忆插件
