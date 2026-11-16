# -*- coding: utf-8 -*-
"""deploy-windows 接宿主机 OpenQQ 的接线检查（不启动任何东西）。

跑法：python -m unittest tests.test_openqq_setup
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIT = os.path.join(ROOT, "deploy-windows")


def read(name):
    with open(os.path.join(KIT, name), encoding="utf-8-sig") as f:
        return f.read()


class OpenQQSetupTests(unittest.TestCase):
    def test_readme_pins_the_fork_commit(self):
        readme = read("README.md")
        self.assertIn("https://github.com/MiameChyan/OpenQQ.git", readme)
        self.assertRegex(readme, r"git clone -b dafeiyu https://github\.com/MiameChyan/OpenQQ\.git")
        self.assertRegex(readme, r"git checkout [0-9a-f]{7,40}")

    def test_onebot_port_is_published_on_localhost_only(self):
        compose = read("compose.yaml")
        ports = re.findall(r'"([^"]*:6199)"', compose)
        self.assertTrue(ports, "astrbot 应映射 6199 给宿主机上的协议端")
        for mapping in ports:
            self.assertTrue(mapping.startswith("127.0.0.1:"), f"6199 只能绑本机: {mapping}")

    def test_env_example_documents_the_port(self):
        self.assertRegex(read(".env.example"), r"(?m)^ONEBOT_WS_PORT=6199$")


if __name__ == "__main__":
    unittest.main()
