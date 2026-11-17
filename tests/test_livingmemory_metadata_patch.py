"""用实际外部 API 方法入口核对 JSON/对象元数据，保留权限拒绝。"""
import ast
import asyncio
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('lmem_patch',ROOT/'deploy/community_patches/livingmemory_metadata.py')
patcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(patcher)

# 来自 LivingMemory 2.13.0 core/external_api.py 的原方法（MIT）；保持真实读回与权限顺序。
SOURCE='''import json
class Service:
    async def _load_memory(self,key,memory_id):
        parsed_id=int(memory_id)
        memory=await self.memory_engine.get_memory(parsed_id)
        if not memory:raise ExternalApiError(404,"not_found","Memory not found")
        metadata = memory.get("metadata") or {}
        if not self._memory_visible(key, metadata):
            raise ExternalApiError(403,"forbidden","Memory is outside this key scope")
        return parsed_id,memory
'''


class Error(Exception):
    def __init__(self,status,*args):self.status=status


class LivingMetadata(unittest.TestCase):
    def service(self,source,metadata,allowed=True):
        namespace={'ExternalApiError':Error};exec(source,namespace)
        service=namespace['Service']()
        async def get(mid):return {'text':'接口自检','metadata':metadata}
        service.memory_engine=SimpleNamespace(get_memory=get)
        service._memory_visible=lambda key,value:allowed and value.get('session_id')=='livingmemory:global'
        return service

    def test_old_method_fails_on_string_then_patched_entry_reads_both(self):
        data={'session_id':'livingmemory:global'}
        with self.assertRaises(AttributeError):asyncio.run(self.service(SOURCE,json.dumps(data))._load_memory(None,1))
        fixed=SOURCE.replace(patcher.OLD,patcher.NEW)
        for value in (data,json.dumps(data)):
            _,memory=asyncio.run(self.service(fixed,value)._load_memory(None,1))
            self.assertEqual(memory['metadata'],data)

    def test_normalization_does_not_bypass_scope(self):
        fixed=SOURCE.replace(patcher.OLD,patcher.NEW)
        with self.assertRaises(Error) as caught:
            asyncio.run(self.service(fixed,'{"session_id":"private-other"}')._load_memory(None,1))
        self.assertEqual(caught.exception.status,403)

    def test_invalid_and_idempotent_patching_preserve_newlines(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'api.py';path.write_bytes(SOURCE.replace('\n','\r\n').encode())
            self.assertTrue(patcher.patch(path));self.assertFalse(patcher.patch(path))
            self.assertEqual(path.read_bytes().count(b'\n'),path.read_bytes().count(b'\r\n'))
        fixed=SOURCE.replace(patcher.OLD,patcher.NEW)
        for bad in ('not-json','[]'):
            with self.assertRaises(Error) as caught:asyncio.run(self.service(fixed,bad)._load_memory(None,1))
            self.assertEqual(caught.exception.status,500)
