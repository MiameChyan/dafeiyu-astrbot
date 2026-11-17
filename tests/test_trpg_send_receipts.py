"""发送回执按异步调用隔离；供插件撤回已发送的消息。"""
import asyncio
import unittest

import test_dafeiyu_hooks as harness_module
from test_dafeiyu_hooks import FakeChain


class SendReceipts(unittest.TestCase):
    def setUp(self):
        harness=harness_module.AiocqhttpPatchTests();harness.setUp()
        self.hooks,self.cls,self.seg=harness.hooks,harness.cls,harness.seg

    def test_receipts_are_real_ids_and_isolated(self):
        async def go():
            class Bot:
                async def send_group_msg(self,**params):
                    await asyncio.sleep(.001)
                    return {'message_id':params['group_id']+1}
            async def send(target):
                captured=[];token=self.hooks.send_receipts.set(captured)
                try:await self.cls.send_message(Bot(),FakeChain([self.seg['Plain']('测试')]),is_group=True,session_id=str(target))
                finally:self.hooks.send_receipts.reset(token)
                return captured
            values=await asyncio.gather(send(1111111111),send(2222222222))
            self.assertEqual(values,[[1111111112],[2222222223]])
            self.assertIsNone(self.hooks.send_receipts.get())
        asyncio.run(go())

    def test_failed_send_has_no_receipt(self):
        async def go():
            captured=[];token=self.hooks.send_receipts.set(captured)
            class Bot:
                async def send_private_msg(self,**params):raise RuntimeError('发送失败')
            try:
                with self.assertRaises(RuntimeError):
                    await self.cls.send_message(Bot(),FakeChain([self.seg['Plain']('测试')]),session_id='1111111111')
            finally:self.hooks.send_receipts.reset(token)
            self.assertEqual(captured,[])
        asyncio.run(go())
