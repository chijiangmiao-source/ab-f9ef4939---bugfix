"""复核记录的 SQLite 持久化。

提交被接受（输入合法）后，无论结论可行还是不可行，都保存一条**自包含**
复核记录：该编号的输入、结论（最小重量、故障通道、选择向量、提示语）与
逐校验证据全部内联在自己的行中，供刷新或重启后凭复核编号取回。

证据绝不按“同一块板（同一通道集合）”共享：同一块板后续再次提交只会
新增记录，既有记录的任何字段都不会被覆盖。输入非法的请求在接口层直接
拒绝，不产生记录（旧证据由前端清除）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid

DB_PATH = os.environ.get("APP_DB", "/data/locator.db")

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS submissions (
                review_id   TEXT PRIMARY KEY,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                payload     TEXT NOT NULL,
                conclusion  TEXT NOT NULL
            )
            """
        )
        _migrate_legacy_shared_evidence(conn)


def _migrate_legacy_shared_evidence(conn: sqlite3.Connection) -> None:
    """把旧版“按通道集合共享证据”的数据库升级为每记录自包含。

    旧实现把 feasible/weight/faulty/vector/recompute/message 存在按通道
    集合键共享的 board_evidence 表中（UPSERT 覆盖），导致同板再次提交后，
    旧编号串到新观测的证据。升级时逐条依据记录自己保存的输入重新精确
    求解复算，把证据内联回各自行；无法重算的行冻结其当前共享证据快照。
    完成后删除共享表，此后任何提交都不再能改写既有记录。
    """
    legacy = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='board_evidence'"
    ).fetchone()
    if legacy is None:
        return

    # 延迟导入以避免 storage <-> solver 的初始化耦合；同时兼容
    # “app 目录在 sys.path 中”与“作为 app 包导入”两种运行方式。
    try:
        from solver import ValidationError, conclusion_evidence, solve
    except ImportError:  # pragma: no cover - 取决于导入方式
        from app.solver import ValidationError, conclusion_evidence, solve

    shared: dict[str, dict] = {}
    for row in conn.execute(
        "SELECT board_key, evidence FROM board_evidence"
    ).fetchall():
        shared[row["board_key"]] = json.loads(row["evidence"])

    rows = conn.execute(
        "SELECT review_id, payload, conclusion FROM submissions"
    ).fetchall()
    for row in rows:
        conclusion = json.loads(row["conclusion"])
        evidence_key = conclusion.pop("evidence_key", None)
        if evidence_key is None:
            # 已经是自包含的新格式，无需处理。
            continue

        evidence = None
        try:
            payload = json.loads(row["payload"])
            channels = payload.get("channels", [])
            check_objs = payload.get("checks", [])
            result = solve(channels, check_objs)
            norm_checks = [
                (tuple(ck["channels"]), int(ck["parity"])) for ck in check_objs
            ]
            evidence = conclusion_evidence(result, norm_checks)
        except (ValidationError, KeyError, TypeError, ValueError):
            # 重算意外失败时冻结该行当前指向的共享证据，至少保证它不再
            # 随后续提交被覆盖。
            evidence = shared.get(evidence_key)

        if evidence:
            conclusion.update(evidence)
        conn.execute(
            "UPDATE submissions SET conclusion = ? WHERE review_id = ?",
            (json.dumps(conclusion, ensure_ascii=False), row["review_id"]),
        )

    conn.execute("DROP TABLE board_evidence")


def save_submission(payload: dict, conclusion: dict) -> str:
    review_id = uuid.uuid4().hex[:12]
    with _lock, _connect() as conn:
        # 极小概率撞号时重试一次。
        while True:
            try:
                conn.execute(
                    "INSERT INTO submissions (review_id, payload, conclusion) "
                    "VALUES (?, ?, ?)",
                    (
                        review_id,
                        json.dumps(payload, ensure_ascii=False),
                        json.dumps(conclusion, ensure_ascii=False),
                    ),
                )
                break
            except sqlite3.IntegrityError:
                review_id = uuid.uuid4().hex[:12]
    return review_id


def load_submission(review_id: str) -> dict | None:
    if not review_id or not all(c in "0123456789abcdef" for c in review_id):
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT review_id, created_at, payload, conclusion "
            "FROM submissions WHERE review_id = ?",
            (review_id,),
        ).fetchone()
    if row is None:
        return None
    # 结论证据内联在记录自身，取回时不依赖任何共享/可变状态。
    return {
        "review_id": row["review_id"],
        "created_at": row["created_at"],
        "input": json.loads(row["payload"]),
        "conclusion": json.loads(row["conclusion"]),
    }
