"""LivingMemory 2.13.0 外部 API 元数据兼容补丁。

只替换核对过的函数片段，支持重复执行；拒绝覆盖其他版本或本地修改。
"""
from pathlib import Path

OLD='''        metadata = memory.get("metadata") or {}
        if not self._memory_visible(key, metadata):
'''
NEW='''        metadata = memory.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (TypeError, ValueError):
                raise ExternalApiError(500, "invalid_metadata", "Memory metadata is invalid") from None
        if not isinstance(metadata, dict):
            raise ExternalApiError(500, "invalid_metadata", "Memory metadata is invalid")
        memory = {**memory, "metadata": metadata}
        if not self._memory_visible(key, metadata):
'''


def patch(path):
    path=Path(path);raw=path.read_bytes();newline=b'\r\n' if raw.count(b'\r\n')>raw.count(b'\n')/2 else b'\n'
    source=raw.decode('utf8').replace('\r\n','\n')
    if NEW in source:return False
    if source.count(OLD)!=1:raise RuntimeError('LivingMemory 外部 API 与已验证版本不一致，未修改')
    path.write_bytes(source.replace(OLD,NEW).replace('\n',newline.decode()).encode('utf8'))
    return True


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('file',type=Path)
    print('已安装兼容补丁' if patch(parser.parse_args().file) else '兼容补丁已存在')
