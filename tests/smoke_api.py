"""Compose verify 使用的 API 冒烟脚本：对运行中的 app 服务发真实请求。

覆盖：健康检查、唯一故障定位、多解同重量裁决、不可行结论持久化、
非法输入（可定位拒绝）、复核取回。任一步失败以退出码 1 结束。
"""

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("APP_URL", "http://app:8080").rstrip("/")


def call(method, path, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"冒烟失败: {name} {detail}")


def main():
    print(f"API 冒烟目标: {BASE}")

    # 0. 健康检查
    status, body = call("GET", "/healthz")
    check("健康检查 200", status == 200 and body.get("status") == "ok")

    # 1. 唯一故障：仅 CH3
    status, data = call("POST", "/api/submit", {
        "channels": ["CH0", "CH1", "CH2", "CH3", "CH4", "CH5"],
        "checks": [
            {"channels": ["CH0", "CH1", "CH3"], "parity": 1},
            {"channels": ["CH2", "CH3", "CH4"], "parity": 1},
            {"channels": ["CH3", "CH5"], "parity": 1},
            {"channels": ["CH0", "CH2", "CH4"], "parity": 0},
        ],
    })
    check("唯一故障提交 200", status == 200, str(data))
    check("唯一故障 = CH3 且重量 1",
          data["conclusion"]["faulty"] == ["CH3"]
          and data["conclusion"]["weight"] == 1,
          str(data["conclusion"].get("faulty")))
    check("逐校验复算全部一致",
          all(r["pass"] for r in data["conclusion"]["recompute"]))
    rid_unique = data["review_id"]

    # 2. 多解裁决：{a,b,c} 奇偶 1 有三个重量 1 解，
    #    选择向量标准字典序裁决给 c（(0,0,1) 最小）。
    status, data = call("POST", "/api/submit", {
        "channels": ["a", "b", "c"],
        "checks": [{"channels": ["a", "b", "c"], "parity": 1}],
    })
    check("多解提交 200", status == 200, str(data))
    check("多解同重量裁决给 c", data["conclusion"]["faulty"] == ["c"],
          str(data["conclusion"].get("faulty")))

    # 3. 不可行：结论必须持久化而非返回近似集合
    status, data = call("POST", "/api/submit", {
        "channels": ["a", "b", "c"],
        "checks": [
            {"channels": ["a", "b"], "parity": 0},
            {"channels": ["a", "b", "c"], "parity": 0},
            {"channels": ["c"], "parity": 1},
        ],
    })
    check("不可行提交 200（保存不可行结论）", status == 200, str(data))
    check("结论为不可行且无故障集合",
          data["conclusion"]["feasible"] is False
          and data["conclusion"]["faulty"] == [],
          str(data.get("conclusion")))
    rid_infeasible = data["review_id"]

    # 4. 刷新后取回两条记录
    status, got = call("GET", f"/api/review/{rid_unique}")
    check("唯一故障记录可取回", status == 200
          and got["conclusion"]["faulty"] == ["CH3"])
    status, got = call("GET", f"/api/review/{rid_infeasible}")
    check("不可行记录可取回且仍不可行",
          status == 200 and got["conclusion"]["feasible"] is False)

    # 4b. 同一通道集合的两次合法观测：两条复核编号必须各自独立。
    #     第一次仅 A 失效，第二次仅 C 失效；首条编号不得被后一次覆盖。
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
    status, first = call("POST", "/api/submit", obs_only_a)
    check("第一次观测提交 200", status == 200, str(first))
    check("第一次观测定位 A", first["conclusion"]["faulty"] == ["A"],
          str(first["conclusion"].get("faulty")))
    rid_first = first["review_id"]

    status, second = call("POST", "/api/submit", obs_only_c)
    check("第二次观测提交 200", status == 200, str(second))
    check("第二次观测定位 C", second["conclusion"]["faulty"] == ["C"],
          str(second["conclusion"].get("faulty")))
    rid_second = second["review_id"]
    check("两次复核编号不同", rid_first != rid_second)

    status, got1 = call("GET", f"/api/review/{rid_first}")
    check("首条记录仍定位 A", status == 200
          and got1["conclusion"]["faulty"] == ["A"],
          str(got1.get("conclusion")))
    check("首条记录输入仍为第一次提交", got1["input"] == {
        "channels": channels,
        "checks": obs_only_a["checks"],
    }, str(got1.get("input")))
    check("首条记录选择向量仅 A 置位",
          got1["conclusion"]["vector"] == {"A": 1, "B": 0, "C": 0},
          str(got1["conclusion"].get("vector")))
    check("首条记录逐校验复算与自身输入一致",
          [(r["members"], r["observed"], r["pass"])
           for r in got1["conclusion"]["recompute"]]
          == [(["A", "B"], 1, True), (["A", "C"], 1, True),
              (["B", "C"], 0, True)],
          str(got1["conclusion"].get("recompute")))

    status, got2 = call("GET", f"/api/review/{rid_second}")
    check("第二条记录定位 C", status == 200
          and got2["conclusion"]["faulty"] == ["C"],
          str(got2.get("conclusion")))
    check("第二条记录输入为第二次提交", got2["input"] == {
        "channels": channels,
        "checks": obs_only_c["checks"],
    }, str(got2.get("input")))
    check("第二条记录逐校验复算与自身输入一致",
          [(r["members"], r["observed"], r["pass"])
           for r in got2["conclusion"]["recompute"]]
          == [(["A", "B"], 0, True), (["A", "C"], 1, True),
              (["B", "C"], 1, True)],
          str(got2["conclusion"].get("recompute")))

    # 5. 非法输入：可定位拒绝（重复通道 / 空集合 / 非法奇偶）
    status, data = call("POST", "/api/submit", {
        "channels": ["a", "b", "a"],
        "checks": [{"channels": [], "parity": 9}],
    })
    check("非法输入返回 400", status == 400, str(status))
    fields = {e["field"] for e in data.get("errors", [])}
    check("错误信息可定位（channels[2]）", "channels[2]" in fields, str(fields))
    check("错误信息可定位（空集合）",
          any(f.endswith(".channels") for f in fields), str(fields))
    check("错误信息可定位（parity）",
          any(f.endswith(".parity") for f in fields), str(fields))

    # 6. 重复校验集合被拒绝
    status, data = call("POST", "/api/submit", {
        "channels": ["a", "b", "c"],
        "checks": [
            {"channels": ["a", "b"], "parity": 0},
            {"channels": ["b", "a"], "parity": 1},
        ],
    })
    check("重复校验集合返回 400", status == 400)
    check("重复原因可读", any("重复" in e["message"] for e in data.get("errors", [])))

    print("API 冒烟全部通过")


if __name__ == "__main__":
    main()
