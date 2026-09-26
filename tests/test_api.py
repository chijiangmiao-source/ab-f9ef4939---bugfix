"""接口冒烟测试：提交、复核取回、非法输入拒绝、不可行结论持久化、健康检查。

以 Python 标准库 urllib 起真实 HTTP 线程，无需第三方依赖。
"""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

# 让测试可以独立运行；且必须在导入应用模块前设置 DB 路径
# （storage 在导入时读取 APP_DB）。
import sys
import tempfile

_TMP = tempfile.TemporaryDirectory()
os.environ["APP_DB"] = os.path.join(_TMP.name, "test.db")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import server  # noqa: E402


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.init_db()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _req(self, method, path, body=None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_healthz(self):
        status, body = self._req("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_index_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers["Content-Type"])
            self.assertIn("故障定位", resp.read().decode("utf-8"))

    def test_submit_unique_fault_and_review(self):
        body = {
            "channels": ["CH0", "CH1", "CH2", "CH3", "CH4", "CH5"],
            "checks": [
                {"channels": ["CH0", "CH1", "CH3"], "parity": 1},
                {"channels": ["CH2", "CH3", "CH4"], "parity": 1},
                {"channels": ["CH3", "CH5"], "parity": 1},
                {"channels": ["CH0", "CH2", "CH4"], "parity": 0},
            ],
        }
        status, data = self._req("POST", "/api/submit", body)
        self.assertEqual(status, 200, data)
        rid = data["review_id"]
        self.assertTrue(rid)
        self.assertEqual(data["conclusion"]["faulty"], ["CH3"])
        self.assertEqual(data["conclusion"]["weight"], 1)
        self.assertTrue(all(r["pass"] for r in data["conclusion"]["recompute"]))

        # 刷新后凭编号取回
        status2, got = self._req("GET", f"/api/review/{rid}")
        self.assertEqual(status2, 200)
        self.assertEqual(got["review_id"], rid)
        self.assertEqual(got["conclusion"]["faulty"], ["CH3"])

    def test_same_board_records_stay_isolated(self):
        """同一通道集合先后两次合法提交：两条复核编号必须各自永久对应
        自己的输入、故障通道、选择向量与逐校验证据，互不被对方覆盖。"""
        channels = ["A", "B", "C"]
        obs_only_a = {
            "channels": channels,
            "checks": [
                {"channels": ["A", "B"], "parity": 1},
                {"channels": ["A", "C"], "parity": 1},
                {"channels": ["B", "C"], "parity": 0},
            ],
        }
        obs_only_c = {
            "channels": channels,
            "checks": [
                {"channels": ["A", "B"], "parity": 0},
                {"channels": ["A", "C"], "parity": 1},
                {"channels": ["B", "C"], "parity": 1},
            ],
        }
        status, first = self._req("POST", "/api/submit", obs_only_a)
        self.assertEqual(status, 200, first)
        self.assertEqual(first["conclusion"]["faulty"], ["A"])
        rid_first = first["review_id"]

        # 不改变通道集合，提交第二组同样合法但应定位 C 的观测
        status, second = self._req("POST", "/api/submit", obs_only_c)
        self.assertEqual(status, 200, second)
        self.assertEqual(second["conclusion"]["faulty"], ["C"])
        rid_second = second["review_id"]
        self.assertNotEqual(rid_first, rid_second)

        # 再次打开首条编号：结论与逐校验证据必须仍属于第一次观测
        status, got1 = self._req("GET", f"/api/review/{rid_first}")
        self.assertEqual(status, 200)
        self.assertEqual(got1["conclusion"]["faulty"], ["A"])
        self.assertEqual(got1["conclusion"]["weight"], 1)
        self.assertEqual(got1["conclusion"]["vector"], {"A": 1, "B": 0, "C": 0})
        self.assertEqual(got1["input"]["checks"], obs_only_a["checks"])
        self.assertEqual(
            [(r["members"], r["observed"], r["pass"])
             for r in got1["conclusion"]["recompute"]],
            [(["A", "B"], 1, True), (["A", "C"], 1, True), (["B", "C"], 0, True)],
        )

        # 第二条记录独立定位 C，复算与自身输入一致
        status, got2 = self._req("GET", f"/api/review/{rid_second}")
        self.assertEqual(status, 200)
        self.assertEqual(got2["conclusion"]["faulty"], ["C"])
        self.assertEqual(got2["conclusion"]["vector"], {"A": 0, "B": 0, "C": 1})
        self.assertEqual(got2["input"]["checks"], obs_only_c["checks"])
        self.assertEqual(
            [(r["members"], r["observed"], r["pass"])
             for r in got2["conclusion"]["recompute"]],
            [(["A", "B"], 0, True), (["A", "C"], 1, True), (["B", "C"], 1, True)],
        )

        # 模拟服务重启：重新执行启动初始化后，既有记录不得发生任何变化。
        server.init_db()
        status, re1 = self._req("GET", f"/api/review/{rid_first}")
        self.assertEqual(status, 200)
        self.assertEqual(re1["conclusion"]["faulty"], ["A"])
        self.assertEqual(re1["conclusion"]["vector"], {"A": 1, "B": 0, "C": 0})
        self.assertTrue(all(r["pass"] for r in re1["conclusion"]["recompute"]))
        status, re2 = self._req("GET", f"/api/review/{rid_second}")
        self.assertEqual(re2["conclusion"]["faulty"], ["C"])

    def test_infeasible_record_not_overwritten_by_same_board(self):
        """不可行结论按各自提交保存：同板再次提交不得改写既有不可行记录。"""
        channels = ["a", "b", "c"]
        infeasible = {
            "channels": channels,
            "checks": [
                {"channels": ["a", "b"], "parity": 0},
                {"channels": ["a", "b", "c"], "parity": 0},
                {"channels": ["c"], "parity": 1},
            ],
        }
        feasible = {
            "channels": channels,
            "checks": [{"channels": ["a", "b", "c"], "parity": 1}],
        }
        status, first = self._req("POST", "/api/submit", infeasible)
        self.assertEqual(status, 200)
        self.assertFalse(first["conclusion"]["feasible"])
        rid_first = first["review_id"]

        status, second = self._req("POST", "/api/submit", feasible)
        self.assertEqual(status, 200)
        self.assertEqual(second["conclusion"]["faulty"], ["c"])

        status, got = self._req("GET", f"/api/review/{rid_first}")
        self.assertEqual(status, 200)
        self.assertFalse(got["conclusion"]["feasible"])
        self.assertEqual(got["conclusion"]["faulty"], [])
        self.assertEqual(got["conclusion"]["recompute"], [])
        self.assertIn("不可行", got["conclusion"]["message"])

    def test_infeasible_is_persisted(self):
        body = {
            "channels": ["a", "b", "c"],
            "checks": [
                {"channels": ["a", "b"], "parity": 0},
                {"channels": ["a", "b", "c"], "parity": 0},
                {"channels": ["c"], "parity": 1},
            ],
        }
        status, data = self._req("POST", "/api/submit", body)
        self.assertEqual(status, 200)
        self.assertFalse(data["conclusion"]["feasible"])
        rid = data["review_id"]
        status2, got = self._req("GET", f"/api/review/{rid}")
        self.assertEqual(status2, 200)
        self.assertFalse(got["conclusion"]["feasible"])
        self.assertEqual(got["conclusion"]["faulty"], [])

    def test_invalid_input_rejected_with_location(self):
        # 重复通道 + 空校验集合 + 非法奇偶
        body = {
            "channels": ["a", "b", "a"],
            "checks": [{"channels": [], "parity": 9}],
        }
        status, data = self._req("POST", "/api/submit", body)
        self.assertEqual(status, 400)
        fields = {e["field"] for e in data["errors"]}
        self.assertIn("channels[2]", fields)
        self.assertTrue(any(".channels" in f for f in fields))
        self.assertTrue(any(".parity" in f for f in fields))

    def test_duplicate_check_set_rejected(self):
        body = {
            "channels": ["a", "b", "c"],
            "checks": [
                {"channels": ["a", "b"], "parity": 0},
                {"channels": ["b", "a"], "parity": 1},
            ],
        }
        status, data = self._req("POST", "/api/submit", body)
        self.assertEqual(status, 400)
        self.assertTrue(any("重复" in e["message"] for e in data["errors"]))

    def test_bad_json_rejected(self):
        req = urllib.request.Request(
            self.base + "/api/submit",
            data=b"{not json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("应返回 400")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)
            data = json.loads(e.read().decode("utf-8"))
            self.assertEqual(data["field"], "body")

    def test_unknown_review_id_404(self):
        status, data = self._req("GET", "/api/review/deadbeefdead")
        self.assertEqual(status, 404)
        self.assertEqual(data["field"], "review_id")


if __name__ == "__main__":
    unittest.main(verbosity=2)
