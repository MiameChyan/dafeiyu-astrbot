# 社区插件兼容补丁

## LivingMemory 2.13.0 外部 API 元数据

外部 API 的 `_load_memory()` 原先把底层返回的 JSON 字符串 metadata 当作对象。写入与幂等成功后，外部读回／归档会在权限检查中触发 AttributeError，返回 500。

`livingmemory_metadata.py` 先规范化 metadata，再保留原来的 scope 权限检查；对象格式照常通过，非法 JSON／非对象返回固定 `invalid_metadata` 错误，不回显原文。补丁重复运行无副作用，保留原换行，找不到唯一已知片段时拒绝修改。它不创建密钥、不修改权限或用户记忆。

只从审阅后 main 使用此脚本。先备份实际安装插件的 `core/external_api.py`，再对该文件执行：

```powershell
python deploy/community_patches/livingmemory_metadata.py <实际安装目录>/core/external_api.py
```

在 Docker 内，复制脚本到临时目录后执行相同命令。不要批量替换整个 LivingMemory 插件。随后只重载已确认的 LivingMemory 插件名称；不能用未知名称试探重载接口，部分 AstrBot 版本会因此重载全部插件。

使用本机保管的 TRPG 写入密钥完成应用状态、写入、幂等、读回及归档检查，再验证真实召回的人格／作用域。密钥与响应原文不要写入日志或审查报告。测试只归档按自检幂等键严格筛选的自检记录，不清理真实记忆。现有 `tests/test_livingmemory_metadata_patch.py` 验证方法入口、权限拒绝、非法数据、幂等和换行；线上读回仍应在实际安装后验证。
