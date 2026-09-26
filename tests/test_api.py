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

    def test_same_board_two_observations_stay_isolated(self):
        # 同一块板（通道集合不变）两次合法观测：先仅 A 失效，再仅 C 失效。
        # 每个复核编号必须永久对应自己的输入/向量/逐校验复算；后续提交
        # 与重新取回都不得把首条编号的证据覆盖成第二次观测。
        sets = [["A", "B"], ["A", "C"], ["B", "C"]]

        def body(parities):
            return {
                "channels": ["A", "B", "C"],
                "checks": [
                    {"channels": m, "parity": p}
                    for m, p in zip(sets, parities)
                ],
            }

        status, data_a = self._req("POST", "/api/submit", body([1, 1, 0]))
        self.assertEqual(status, 200, data_a)
        rid_a = data_a["review_id"]
        self.assertEqual(data_a["conclusion"]["faulty"], ["A"])

        status, data_c = self._req("POST", "/api/submit", body([0, 1, 1]))
        self.assertEqual(status, 200, data_c)
        rid_c = data_c["review_id"]
        self.assertNotEqual(rid_a, rid_c)
        self.assertEqual(data_c["conclusion"]["faulty"], ["C"])

        # 重新打开/刷新首条编号：仍须定位 A，且输入与逐校验复算属于观测 1。
        status, got_a = self._req("GET", f"/api/review/{rid_a}")
        self.assertEqual(status, 200)
        self.assertEqual(got_a["input"], body([1, 1, 0]))
        ca = got_a["conclusion"]
        self.assertEqual(ca["faulty"], ["A"])
        self.assertEqual(ca["weight"], 1)
        self.assertEqual(ca["vector"], {"A": 1, "B": 0, "C": 0})
        self.assertIn("A", ca["message"])
        self.assertEqual(
            [(r["observed"], r["recomputed"], r["pass"]) for r in ca["recompute"]],
            [(1, 1, True), (1, 1, True), (0, 0, True)],
        )

        # 第二条独立定位 C，证据属于观测 2。
        status, got_c = self._req("GET", f"/api/review/{rid_c}")
        self.assertEqual(status, 200)
        self.assertEqual(got_c["input"], body([0, 1, 1]))
        cc = got_c["conclusion"]
        self.assertEqual(cc["faulty"], ["C"])
        self.assertEqual(cc["vector"], {"A": 0, "B": 0, "C": 1})
        self.assertEqual(
            [(r["observed"], r["recomputed"], r["pass"]) for r in cc["recompute"]],
            [(0, 0, True), (1, 1, True), (1, 1, True)],
        )

    def test_unknown_review_id_404(self):
        status, data = self._req("GET", "/api/review/deadbeefdead")
        self.assertEqual(status, 404)
        self.assertEqual(data["field"], "review_id")


if __name__ == "__main__":
    unittest.main(verbosity=2)
