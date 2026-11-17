# AstrBot QQ 机器人 · Windows 本机部署实践

在一台 Windows 电脑上，用 Docker 跑 [AstrBot](https://github.com/AstrBotDevs/AstrBot)，接一个 QQ 小号，做一个能私聊、能在群里自然接话、有长期记忆的聊天机器人。本仓库记录这套部署怎么搭起来、用了哪些组件、为什么这样选，以及一路踩过的坑。

本仓库 fork 自 [whyever77/dafeiyu-astrbot](https://github.com/whyever77/dafeiyu-astrbot)。上游是一套部署在云服务器上的完整方案；本 fork 只保留本机部署用得到的部分，并换了协议端、记忆插件和群聊插件。人格文本放在 [`persona/`](persona/)，这里不展开。

## 整体结构

```
QQ 小号 ── 桌面 QQ NT + OpenQQ（宿主机）── 反向 WS 127.0.0.1:6199 ──┐
                                                                   ▼
                        Docker Desktop：AstrBot v4.29.0（核心补丁 bind-mount）
                          ├─ 对话模型：DeepSeek（deepseek-flash，能看图）
                          ├─ 嵌入：本机 Ollama bge-m3（host.docker.internal:11434）
                          ├─ 长期记忆：LivingMemory
                          ├─ 群聊读空气：group_chat_plus
                          ├─ 知识库：AstrBot 自带（热梗词典等）
                          └─ 本仓库插件：face_convert / chat_rhythm / schedule_awareness
宿主机：掉线监控 watchdog.py（只弹通知，不自动重启）
```

## 组件与选择

| 角色 | 用的是什么 | 为什么 |
|---|---|---|
| 宿主 | AstrBot **v4.29.0**，Docker Desktop（WSL2） | 版本钉死：仓库里的核心补丁是整文件替换，换版本要重打 |
| QQ 协议端 | [OpenQQ](https://github.com/feijx3/OpenQQ) 的 fork [MiameChyan/OpenQQ](https://github.com/MiameChyan/OpenQQ)（`dafeiyu` 分支） | NapCat 于 2026-11 停更，小号还连续被风控。OpenQQ 全部开源、没有预编译二进制、只连本机。fork 补了图片内嵌、私聊昵称、`get_msg`、@ 唤醒等功能。NapCat 留作备用（compose 的 `napcat` profile） |
| 对话模型 | DeepSeek `deepseek-flash` | 便宜，能识图 |
| 嵌入 | 本机 Ollama `bge-m3`（1024 维） | 聊天原文只在本机做嵌入，不发给第三方 |
| 长期记忆 | [LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) 2.13.0 | 社区最成熟的记忆插件，自带管理面板，自动清理只归档不删除。作用域用 `global`，管理员私聊设为隔离会话 |
| 群聊 | [group_chat_plus](https://github.com/Him666233/astrbot_plugin_group_chat_plus) V1.2.3.hotfix.2 | 先按概率筛一遍，再让模型读空气决定要不要接话，专门适配了 LivingMemory。线上打了一个本地补丁（见下文） |
| 知识库 | AstrBot 自带 | 能建多个库、格式多、带重排序；不用记忆插件自带的知识库 |
| 表情包 | [stealer](https://github.com/nagatoquin33/astrbot_plugin_stealer) 3.2.2 | 收集群里的表情包；开启人工审核，第三方打分不开 |

自建的长期记忆系统做到了 M1（存储与捕获），2026-11-17 改用开源插件后归档，见 [MiameChyan/dafeiyu-memory-system](https://github.com/MiameChyan/dafeiyu-memory-system)（只读）。原则是能用现成插件的就不自己写；接下来只自己做跑团（TRPG）插件。

## 本仓库提供的东西

| 路径 | 内容 |
|---|---|
| [`deploy-windows/`](deploy-windows/) | Windows 部署套件：compose、首次部署脚本、日常启停、数据卷迁移、掉线监控。**从这里开始** |
| [`deploy/Dockerfile`](deploy/Dockerfile) | AstrBot 镜像（官方镜像 + 少量依赖） |
| [`deploy/patches/`](deploy/patches/) | AstrBot v4.29.0 核心补丁：按语义分气泡发送、拟人节奏与出站限速、挂钩点（TRPG 入站观察、同步分流与出站通知）、语音合成（暂未启用） |
| [`deploy/heal_ws.py`](deploy/heal_ws.py) | NapCat 备用时的 WS 断线自愈 |
| [`plugin/astrbot_plugin_face_convert/`](plugin/astrbot_plugin_face_convert/) | 出站：表情名转真 QQ 表情、合并气泡、清理草稿和回声；入站：图片、语音、视频转述，给群聊消息加身份标记 |
| [`plugin/astrbot_plugin_chat_rhythm/`](plugin/astrbot_plugin_chat_rhythm/) | 私聊连发合并（等对方说完再回）；纯表情包按概率不回 |
| [`plugin/astrbot_plugin_schedule_awareness/`](plugin/astrbot_plugin_schedule_awareness/) | 把会话名下的定时任务注入系统提示词 |
| [`plugin/astrbot_plugin_affective_core/`](plugin/astrbot_plugin_affective_core/) | 情感状态插件，写好了还没部署 |
| [`lib/dafeiyu-shared/`](lib/dafeiyu-shared/) | TRPG 插件可能复用的基础库 |
| [`persona/`](persona/) | 人设、人格提示词和课程文件 |
| [`docs/`](docs/) | [踩坑合集](docs/踩坑合集.md)、[部署清单](docs/伽拉忒亚-部署清单.md)、[指令手册](docs/指令手册.md)、知识库与导入说明 |
| [`tests/`](tests/) | 回归测试：`python -m unittest discover -s tests -p "test_*.py"` |

## 部署步骤

完整步骤见 [`deploy-windows/README.md`](deploy-windows/README.md)。大致顺序：

1. 装 Docker Desktop（WSL2 后端）和 Ollama（`ollama pull bge-m3`）。
2. 按说明 clone 并构建 OpenQQ fork，放在**纯英文路径**下。
3. 首次部署：`deploy-windows\start.ps1`。它会建好数据卷，构建并启动 AstrBot。
4. 在 AstrBot 面板里配好消息平台（aiocqhttp 反向 WS 6199）、DeepSeek 和嵌入提供商，导入人格。
5. 把本仓库 `plugin/` 下需要的插件拷进 `deploy-windows\data\plugins\`，再从插件市场或 release 包装 LivingMemory、group_chat_plus、stealer。
6. 以后日常就用根目录的 `启动大肥鱼.ps1` / `安全关闭大肥鱼.ps1`。

## 实践记录（摘要）

更完整的记录在 [踩坑合集](docs/踩坑合集.md) 和 [CHANGELOG](CHANGELOG.md)。

- **SQLite 不要放在 Docker Desktop 的 Windows 绑定挂载上。** WAL 共享内存在这种挂载上不可靠，数据库会突然写不进去（`disk I/O error`），对话历史停更，机器人就开始答非所问。AstrBot 数据改放 Docker 数据卷，只有插件代码和插件配置仍从宿主机挂进去。
- **容器里收不到 Windows 绑定挂载的文件变动事件**（9p）：在宿主机上改文件，容器里的 watchdog 一个事件也收不到。要监视这类目录只能轮询。
- **插件初始化早于 Provider 加载。** 插件在 `initialize` 里找嵌入或模型 Provider 会落空，要等 `on_astrbot_loaded` 再取。
- **AstrBot 在容器里是 PID 1，只处理 SIGINT。** compose 要设 `stop_signal: SIGINT`，否则每次 `docker stop` 都要等到超时，被强杀。
- **第三方插件装之前先看源码**：有没有外发地址、会不会开端口、默认配置是什么。group_chat_plus 默认对**所有群**生效，还默认开着打字模拟和错别字生成，装上就要先收窄范围。
- **group_chat_plus V1.2.3.hotfix.2 的上游 bug**：保存群消息到对话历史时，它的清理规则会把群友的原话一起删掉。上游只在 `beta` 分支修了，没发版；线上把那一行修复移植了过来（见 CHANGELOG），升级时要检查新版有没有带上。
- **NapCat 停更与风控**：两天内被判"身份失效"三次以后，改用挂接桌面 QQ 的 OpenQQ。掉线监控只弹通知、不自动重启：身份失效时快速登录必然失败，反复重启只会更像机器人。
- **长期记忆会写错事实**：自动总结可能把事实写反，机器人还会照着错的记忆行动。所以要定期在 LivingMemory 面板里翻看、修正，重要测试前先暂停记忆。

## 致谢与许可

- 上游项目：[whyever77/dafeiyu-astrbot](https://github.com/whyever77/dafeiyu-astrbot)，本仓库沿用其 MIT 许可（见 [LICENSE](LICENSE)）。
- `deploy/patches/` 下是 AstrBot 源码的修改版，按 AGPL-3.0 随本仓库分发。
- 依赖的开源项目：[AstrBot](https://github.com/AstrBotDevs/AstrBot) · [OpenQQ](https://github.com/feijx3/OpenQQ) · [NapCat](https://github.com/NapNeko/NapCatQQ) · [LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) · [group_chat_plus](https://github.com/Him666233/astrbot_plugin_group_chat_plus) · [stealer](https://github.com/nagatoquin33/astrbot_plugin_stealer) · [Ollama](https://ollama.com)
