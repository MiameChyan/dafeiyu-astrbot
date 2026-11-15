# dafeiyu-shared

伽拉忒亚的记忆系统与 TRPG 插件共用的 Python 基础库：

- `shared.agentloop`：子任务的「生成 → 核查 → 修正」循环、预算、Provider 适配与调用埋点。
- `shared.observability`：关联 ID、结构化日志、指标、审计、提示词版本与回放。

规格见 [日志与观测规格](docs/日志与观测规格.md)。共享库不依赖记忆系统、TRPG 或 AstrBot；AstrBot Provider 通过适配器传入，单元测试不需要安装 AstrBot。

## 被插件引用

记忆系统与 TRPG 仓库用固定提交引用本目录：

```toml
dependencies = [
    "dafeiyu-shared @ git+https://github.com/MiameChyan/dafeiyu-astrbot.git@<提交号>#subdirectory=lib/dafeiyu-shared",
]
```

本仓库公开，安装时不需要额外的访问权限。升级共享库后，同步更新两个插件仓库里的提交号。

## 开发

需要 Python 3.12+ 与 uv。在本目录运行：

```powershell
uv sync
uv run pytest
```
