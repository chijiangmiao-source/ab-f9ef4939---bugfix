"""同板两次观测的复核编号隔离验收（对 Compose 启动的 app 发真实请求）。

场景：同一块硅像素读出板（通道集合恒为 A/B/C，不改变通道集合）
  观测 1：校验奇偶 (1,1,0) —— 唯一最小重量解为“仅 A 失效”；
  观测 2：校验奇偶 (0,1,1) —— 唯一最小重量解为“仅 C 失效”。

两个阶段分别在服务重启前后由 scripts/acceptance.sh 调用：

  phase1 <BASE>  依次提交两次观测，保留首条编号，查询并核对两条记录
                 （输入、选择向量、逐校验复算各自独立且与提交一致），
                 最后一行打印 RID=<首条编号> 供脚本重启后使用。
  phase2 <BASE> <RID_A> [RID_C]
                 服务重启后再次核对首条记录（以及第二条），确认结论
                 仍分别定位 A / C，且证据与各自输入一致。

任一步失败以退出码 1 结束。
"""

import json
import sys
import urllib.error
import urllib.request

CHANNELS = ["A", "B", "C"]
# 两次观测引用的校验集合完全相同，仅观测奇偶不同。
CHECK_SETS = [["A", "B"], ["A", "C"], ["B", "C"]]
OBS1_PARITIES = [1, 1, 0]   # 综合征 = A 的列：仅 A 失效
OBS2_PARITIES = [0, 1, 1]   # 综合征 = C 的列：仅 C 失效]


def build_body(parities):
    return {
        "channels": list(CHANNELS),
        "checks": [
            {"channels": list(members), "parity": p}
            for members, p in zip(CHECK_SETS, parities)
        ],
    }


def call(base, method, path, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"验收失败: {name} {detail}")


def expected_vector(faulty):
    return {ch: (1 if ch == faulty else 0) for ch in CHANNELS}


def normalize_input(body):
    """服务端会把每条校验的通道排序；生成用于比对的期望输入快照。"""
    return {
        "channels": list(CHANNELS),
        "checks": [
            {"channels": sorted(ck["channels"]), "parity": ck["parity"]}
            for ck in body["checks"]
        ],
    }


def assert_record(base, rid, faulty, parities, label):
    """核对一条复核记录：输入、向量、逐校验复算全部与该次提交一致。"""
    status, got = call(base, "GET", f"/api/review/{rid}")
    check(f"{label} 记录可取回", status == 200, str(status))
    check(f"{label} 编号一致", got["review_id"] == rid)

    body = build_body(parities)
    check(f"{label} 保存的输入仍为本次提交",
          got["input"] == normalize_input(body),
          f"{got['input']} != {normalize_input(body)}")

    c = got["conclusion"]
    check(f"{label} 结论可行且重量为 1",
          c["feasible"] is True and c["weight"] == 1, str(c))
    check(f"{label} 故障通道仅 {faulty}", c["faulty"] == [faulty],
          str(c.get("faulty")))

    vec = expected_vector(faulty)
    check(f"{label} 选择向量与故障定位一致", c["vector"] == vec,
          f"{c.get('vector')} != {vec}")
    check(f"{label} 提示语与故障通道一致",
          faulty in c.get("message", ""), c.get("message"))

    rows = c.get("recompute", [])
    check(f"{label} 逐校验复算条数与提交一致",
          len(rows) == len(CHECK_SETS), str(rows))
    for i, (row, members, observed) in enumerate(
            zip(rows, CHECK_SETS, parities)):
        members = sorted(members)
        want = 0
        for name in members:
            want ^= vec[name]
        check(f"{label} 复算第 {i + 1} 条引用集合与输入一致",
              row["members"] == members, str(row))
        check(f"{label} 复算第 {i + 1} 条观测奇偶与输入一致",
              row["observed"] == observed == want,
              f"observed={row['observed']} submitted={observed} xor={want}")
        check(f"{label} 复算第 {i + 1} 条 XOR 与向量一致且通过",
              row["recomputed"] == want and row["pass"] is True, str(row))


def phase1(base):
    print(f"验收阶段 1（提交与隔离核对）目标: {base}")

    # 观测 1：仅 A 失效
    status, data1 = call(base, "POST", "/api/submit", build_body(OBS1_PARITIES))
    check("观测 1 提交 200", status == 200, str(data1))
    check("观测 1 当场定位仅 A",
          data1["conclusion"]["faulty"] == ["A"]
          and data1["conclusion"]["weight"] == 1,
          str(data1["conclusion"].get("faulty")))
    rid_a = data1["review_id"]

    # 观测 2：不改变通道集合，仅 C 失效
    status, data2 = call(base, "POST", "/api/submit", build_body(OBS2_PARITIES))
    check("观测 2 提交 200", status == 200, str(data2))
    check("观测 2 当场定位仅 C",
          data2["conclusion"]["faulty"] == ["C"]
          and data2["conclusion"]["weight"] == 1,
          str(data2["conclusion"].get("faulty")))
    rid_c = data2["review_id"]
    check("两次提交生成不同复核编号", rid_a != rid_c, f"{rid_a} == {rid_c}")

    # 关键回归：再次打开/刷新第一条编号，必须仍是观测 1 的证据，
    # 而不是被同板第二次提交覆盖。
    assert_record(base, rid_a, "A", OBS1_PARITIES, "首条（观测 1）")
    assert_record(base, rid_c, "C", OBS2_PARITIES, "第二条（观测 2）")

    print("阶段 1 验收通过")
    # 最后一行供验收脚本在重启后传回。
    print(f"RID={rid_a} {rid_c}")


def phase2(base, rid_a, rid_c=None):
    print(f"验收阶段 2（服务重启后复核）目标: {base}")
    assert_record(base, rid_a, "A", OBS1_PARITIES, "重启后首条（观测 1）")
    if rid_c:
        assert_record(base, rid_c, "C", OBS2_PARITIES, "重启后第二条（观测 2）")
    print("阶段 2 验收通过")


def main(argv):
    if len(argv) >= 3 and argv[1] == "phase1":
        phase1(argv[2].rstrip("/"))
    elif len(argv) >= 4 and argv[1] == "phase2":
        phase2(argv[2].rstrip("/"), argv[3], argv[4] if len(argv) > 4 else None)
    else:
        raise SystemExit("用法: acceptance_api.py phase1 <BASE> | "
                         "phase2 <BASE> <RID_A> [RID_C]")


if __name__ == "__main__":
    main(sys.argv)
