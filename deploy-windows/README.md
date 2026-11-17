# Windows 本机部署套件（Docker Desktop）

> AstrBot 跑在 Docker Desktop 里；QQ 协议端用宿主机上的 OpenQQ，NapCat 留作备用。
> 宿主路径都是相对路径，端口、时区、QQ 小号等走 `.env`；镜像构建用 `../deploy/Dockerfile`，核心补丁用 `../deploy/patches/`。

## 首次部署

前置：
- Docker Desktop（WSL2 后端，给 4 GB 以上内存）；
- Ollama，并执行 `ollama pull bge-m3`（本机嵌入）；
- 一个 **QQ 小号**；
- Windows 桌面版 QQ NT（OpenQQ 要挂接它）。

```powershell
powershell -ExecutionPolicy Bypass -File deploy-windows\start.ps1
```

`start.ps1` 依次做这些事：检查 Docker → 第一次部署时创建数据卷 `dafeiyu-astrbot-data` → 从 `.env.example` 生成 `.env` → 构建 AstrBot 镜像 → 启动容器 → 等面板就绪。
`.\data` 里如果还有没迁移的旧数据，它会拒绝启动；这时先运行 `migrate-data-volume.ps1`。

### 1. 准备 OpenQQ（QQ 协议端）

NapCat 停更，小号又接连被判「身份失效」，所以 2026-11-16 起改用 [OpenQQ](https://github.com/feijx3/OpenQQ)（审查过上游 `14a7bac`）。
实际用的是我们的 fork [MiameChyan/OpenQQ](https://github.com/MiameChyan/OpenQQ) 的 `dafeiyu` 分支（锁定在 `5f6d71a`），
在上游基础上加了「收到图片时让 QQ 下载原图，并以 base64 内嵌」（否则容器里的 AstrBot 看不到图片）、「私聊消息补上发送者昵称」、「实现 `get_msg`」（AstrBot 解析引用回复要用）和「引用里的图片也下载原图并内嵌」。
它在 **Windows 桌面版 QQ NT** 里挂接会话，和 NapCat / SnowLuma 同属「挂接官方客户端」，只是跑在真机桌面 QQ 上。
代码全部开源、无预编译二进制、除本机外不连任何地址。

- **获取**：`git clone -b dafeiyu https://github.com/MiameChyan/OpenQQ.git`，然后 `git checkout 5f6d71a`。
- **放在纯英文路径**（如 `D:\RAG-ai-code\OpenQQ`）：cmake 4.2 在含中文的路径上直接崩溃（`0xC0000409`）。
- **构建**：`pip install ninja`（装了 g++ 时它强制用 Ninja）→ `npm ci` → `npm run build`。
- **配置** `config/openqq.json`（不进版本控制）：`qqRoot` 填 QQ 安装目录；只开反向 WS
  `ws://127.0.0.1:6199/ws`，正向 WS 关掉。AstrBot 这边 compose 已把 6199 映射到 `127.0.0.1`。
- **启动顺序**：先停掉 NapCat 容器 → **退出所有 QQ** → `node src/main.mjs start` → 在它拉起的 QQ 里扫码登录小号 →
  需要的话再另开 QQ 登录主号（另开的是独立进程，实测没有加载 OpenQQ 的 DLL）。重启 OpenQQ 前两个 QQ 都要退出。
- **图片**：fork 里收到图片时先让 QQ 下载原图（最多等 10 秒），5 MB 以内以 `base64://` 交给 AstrBot。
  实测 162 KB 的照片与消息同一秒到达，模型能看到图片；超时或超限时退回补全的下载地址（NT 新版图片多半 404）。
- **昵称**：上游私聊事件不带昵称；fork 从好友列表或 QQ 资料服务补上（缓存）。
- **引用回复**：上游没有 `get_msg`，AstrBot 看不到被引用的那句；fork 用 QQ 随引用附带的被引用消息副本（`records`）回答，引用多久以前的消息都行；没有副本时再查本次运行的缓存和 QQ 本机历史。
  被引用的消息里有图片时（`5f6d71a`），先下载被引用副本的原图，`get_msg` 返回时重新内嵌，模型才看得到引用里的图。
- **@ 唤醒**：上游没有 `get_stranger_info`，被 @ 的人没设群名片时 AstrBot 会丢掉整个 @ 段，@ 她不会被唤醒；fork 已实现。
- **掉线监控**：`.env` 里填 `OPENQQ_DIR`（OpenQQ 所在目录），`watchdog.py` 就会按 OpenQQ 的规则查（见下面「掉线 / 假在线监控」）。
- **日常启停**：用仓库根目录的 `启动大肥鱼.ps1` / `安全关闭大肥鱼.ps1`（说明见 [日常启停.md](日常启停.md)）。
  启动脚本会起 AstrBot、在新窗口拉起 OpenQQ 并等登录完成、确认连上 6199，再开监控；NapCat 在跑会先停掉，避免两边顶号。
  NapCat 已放进 compose 的 `napcat` profile，普通的 `up` / `start` 不会再带上它。

### 2. 配 AstrBot

打开 `http://127.0.0.1:6185`。初始账号密码只打印在日志里，登录后立刻改密码：

```powershell
docker compose logs astrbot | Select-String "Initial"
```

- **消息平台**：新增 QQ 个人号（aiocqhttp），反向 WS 主机 `0.0.0.0`、端口 `6199`。
- **对话模型**：建 DeepSeek 源（`https://api.deepseek.com/v1` + key），**还要在源下添加模型**（`deepseek-flash`）并设为默认，否则会报「未找到任何可用的对话模型」。
- **嵌入**：新增**嵌入类型**的提供商（挂在对话源下面的模型会被当成聊天模型，知识库和记忆插件都选不到）：
  - 本机 Ollama（推荐，原文不出本机）：`type: openai_embedding`，`embedding_api_base: http://host.docker.internal:11434/v1`，`embedding_model: bge-m3`，维度 1024，`embedding_dimensions_mode: never`，key 随便填（如 `ollama`）；
  - 或硅基流动托管：`embedding_api_base: https://api.siliconflow.cn/v1`，`embedding_model: BAAI/bge-m3`。
- **配置**：`platform_settings.ignore_bot_self_message` 设为 `true`；`empty_mention_waiting` 设为 `false`（group_chat_plus 要求）；`provider_ltm_settings.group_icl_enable`（群聊上下文感知）设为 `true`。
- **人格**：新建人格，系统提示词为 [人格提示词](../persona/伽拉忒亚-人格提示词.md) 中 `---` 之后的正文，设为默认（v4.29.0 读的是 `agent_runner.config.persona.persona_id`）。

### 3. 插件

本仓库的插件：把 `plugin/` 下的文件夹拷进 `data\plugins\`，重启 AstrBot。

- `astrbot_plugin_face_convert`：表情、气泡、身份标记、媒体转述。
- `astrbot_plugin_schedule_awareness`：定时任务注入。
- `astrbot_plugin_chat_rhythm`：私聊里对方连发几条时，等 4 秒静默、并且对方不在打字，再一次回复；一直在打字最多等 15 秒。这一批全是表情包（不含普通照片）时，有 60% 的概率不回，连发几张时每多一张减半。以 `/`、`.`、`。` 开头的指令和群聊不受影响。

社区插件（装前都看过源码；版本号是审查过的版本）：

- **LivingMemory 2.13.0**（长期记忆）：`provider_settings.embedding_provider_id` 填上面的 Ollama 嵌入；`filtering_settings.memory_scope_mode` 设为 `global`，`isolated_sessions` 填管理员私聊的完整会话 ID（如 `default:FriendMessage:<QQ号>`），私聊内容就不会被带进群里。自动清理保持「归档而非删除」。
- **group_chat_plus V1.2.3.hotfix.2**（群聊读空气）：
  - 默认对**所有群**生效，先在 `enabled_groups` 里只填一个群试用；
  - `trigger_keywords` 填机器人的名字和常见写法，并开启 `keyword_smart_mode`，命中后仍要过读空气判断；
  - 私聊、主动对话、Web 面板保持关闭；
  - **本地补丁**：这个版本的 `utils/message_cleaner.py` 会把群友原话从对话历史里删掉，要把上游 `beta` 分支的那一行清理规则移植过来，详见 CHANGELOG。
- **表情包小偷**（`nagatoquin33/astrbot_plugin_stealer`，审查过 v3.2.2 / `6a993b2`）：开 `steal_meme`；`vision_provider_id` 填对话模型；保持 `audit_required`（偷来的图人工通过才入库）。JEV / TypeSafe 留空，否则对话片段会发给第三方。

### 4. 活人感相关配置（2026-11-16 实测）

- **分段回复**：`platform_settings.segmented_reply` 设 `enable: true`、`max_segments: 3`、`only_llm_result: true`，`regex` 改成 `.*?[。？！?!~…\n]+|.+$`。人格句末大多不打标点、靠换行分条，默认正则只按句号切，会把两行话塞进同一个气泡。`rate_limit.count` 降到 `10`。
- **热梗词典**：AstrBot 知识库，导入 [热梗词典](../docs/知识库-热梗词典示例.md)，`kb_names: ["热梗词典"]`，`kb_agentic_mode: true`（按需检索）。默认模式不设分数门槛，每条消息都会把 5 块词典塞进上下文；按需模式下，模型遇到不认识的说法才调 `astr_kb_search`。

### 5. NapCat（备用）

NapCat 放在 compose 的 `napcat` profile 里，普通的 `up` / `start` 不会带上它。确实要用时：

```powershell
docker load -i napcat-docker-v4.18.42-amd64.tar   # 先核对 sha256
docker compose --profile napcat up -d napcat
```

NapCat 和 OpenQQ 登的是同一个小号，两边同时在线会互相顶号，用之前先关掉 OpenQQ。镜像来源与核验见 [NapCat 停更与镜像来源](../docs/NapCat停更与镜像来源.md)。WebUI 在 `http://127.0.0.1:6099/webui`，断线时可用 `python ..\deploy\heal_ws.py` 自愈。

### 6. 预留给 TRPG 的端口

compose 还把海豹 WebUI 映射到了 `127.0.0.1:3211`，这是给计划中的 TRPG 骰子插件预留的：海豹会作为它的子进程跑在 AstrBot 容器里。插件还没做，现在这个端口用不上。

## 日常运维

```powershell
..\启动大肥鱼.ps1                   # 日常启动：AstrBot + OpenQQ + 掉线监控
..\安全关闭大肥鱼.ps1               # 安全关闭：等 AstrBot 正常退出；OpenQQ 保持登录
.\start.ps1 -NoBuild               # 首次部署 / 重建后起容器
docker compose logs -f astrbot     # 跟日志
```

**数据在 Docker 数据卷 `dafeiyu-astrbot-data` 里**，不在 `.\data`（2026-11-16 起，原因见 compose.yaml）。
`.\data` 里只有 `plugins\`、`config\` 仍然挂进容器；聊天配置用 AstrBot 面板改。旧数据迁移用 `migrate-data-volume.ps1`，备份方法见 [日常启停.md](日常启停.md)。


### 掉线 / 假在线监控

小号被风控时，有时 NapCat 自己还显示「已登录」，但消息已经收不到了（假在线）。`启动大肥鱼.ps1`（以及首次部署的 `start.ps1`）会在后台用 `pythonw` 拉起 [watchdog.py](watchdog.py)，`安全关闭大肥鱼.ps1` / `stop.ps1` 会先停掉它；PID 记在 `logs\watchdog.pid`，启动脚本据此识别已在运行的监控，不会重复拉起。它每 10 分钟检查一次：

- **查哪个协议端**：`.env` 的 `WATCHDOG_PROTOCOL`，默认 `auto`：OpenQQ 进程在跑就按 OpenQQ 查，否则 NapCat 容器在跑就按 NapCat 查，都没在跑直接报掉线。
- **掉线**（60 秒后复查仍失败才算）：
  - NapCat：WebUI 连不上；显示未登录或已离线；OneBot `get_status` 离线；`nc_get_rkey` 向 QQ 服务器取数据失败。
  - OpenQQ：`node src/main.mjs start` 进程不在；`OPENQQ_DIR\.runtime\status.json` 不是 `session_ready`（被踢下线时 OpenQQ 会写成 `offline`）；
    挂接的 QQ 进程已退出；AstrBot 容器没在跑；AstrBot 容器里 6199 端口没有已建立的连接。
    OpenQQ 的 WebUI 密钥每次启动随机生成，所以不像 NapCat 那样主动调 OneBot 接口探测。
- **疑似假在线**：白天（默认北京时间 9–24 点）连续 3 小时一条消息都没收到（NapCat 看自己的收消息日志，OpenQQ 看 AstrBot 的 `core.event_bus` 日志）。
  协议端或 AstrBot 刚重启不满 3 小时不判。

状态变化时弹 Windows 通知，没恢复就每 2 小时提醒一次，恢复后再通知一次。**只通知，不重启任何东西**：身份失效时快速登录必然失败，反复重启只会更像机器人。日志在 `logs\watchdog.log`，QQ 号、群号等长数字都会打码。

```powershell
python watchdog.py --once          # 手动查一次
python watchdog.py --test-notify   # 试一下通知能不能弹出
.\start.ps1 -NoBuild -NoWatchdog   # 不要监控
```

阈值在 `.env` 里改：`WATCHDOG_INTERVAL_MIN`、`WATCHDOG_SILENCE_HOURS`、`WATCHDOG_ACTIVE_HOURS`、`WATCHDOG_PROTOCOL`、`OPENQQ_DIR`。
## 验证状态（2026-11-17，Windows 11 + Docker Desktop）

- ✅ AstrBot v4.29.0 构建与启动，核心补丁（气泡发送、拟人节奏、挂钩点）挂载到位
- ✅ OpenQQ fork `5f6d71a`：扫码登录小号，反向 WS 连通（6199 ESTABLISHED），图片、引用里的图片、@ 唤醒都正常
- ✅ LivingMemory 2.13.0 加载，嵌入接上本机 Ollama；group_chat_plus 群聊实测：触发词命中 → 读空气 → 回复，完整存进历史（打补丁后）
- ✅ 掉线监控：OpenQQ 模式的掉线与恢复通知都能弹出
- ⏳ LivingMemory 切换到 `global` 后的实际召回效果、group_chat_plus 放开到所有群
