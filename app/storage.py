"""复核记录的 SQLite 持久化。

提交被接受（输入合法）后，无论结论可行还是不可行，都保存一条复核
记录，供刷新后凭复核编号取回。输入非法的请求在接口层直接拒绝，
不产生记录（旧证据由前端清除）。

每条复核记录**自包含**其全部内容：提交输入（通道与校验）以及完整
结论（最小重量、故障通道、选择向量、提示语、逐校验复算、可行性）。
同一硅像素读出板（通道集合相同）的多次提交各自独立保存，后续提交、
页面刷新或服务重启都不会改变既有编号对应的任何内容。
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
        _migrate_legacy_board_evidence(conn)


def _migrate_legacy_board_evidence(conn: sqlite3.Connection) -> None:
    """把旧版“按板共享证据”表合并回各自的提交记录后删除。

    旧实现把故障向量、提示语与逐校验复算保存在以通道集合为键的
    board_evidence 表中，同一板的后续提交会覆盖先前编号的证据。
    新实现要求每条记录自包含；迁移时仅能把最后一次写入的证据归还给
    来源提交（更早且已被覆盖的记录在旧库中本就无法还原）。
    """
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "board_evidence" not in names:
        return

    latest_by_source: dict[str, str] = {}
    for row in conn.execute(
        "SELECT source_review_id, evidence FROM board_evidence"
    ):
        latest_by_source[row[0]] = row[1]

    for row in conn.execute("SELECT review_id, conclusion FROM submissions"):
        conclusion = json.loads(row["conclusion"])
        # 新格式记录已自包含（含 feasible 字段），原样保留。
        if "feasible" in conclusion:
            continue
        conclusion.pop("evidence_key", None)
        evidence = latest_by_source.get(row["review_id"])
        if evidence is not None:
            conclusion.update(json.loads(evidence))
        conn.execute(
            "UPDATE submissions SET conclusion = ? WHERE review_id = ?",
            (json.dumps(conclusion, ensure_ascii=False), row["review_id"]),
        )
    conn.execute("DROP TABLE board_evidence")


def save_submission(payload: dict, conclusion: dict) -> str:
    """保存一条自包含的复核记录，返回复核编号。"""
    review_id = uuid.uuid4().hex[:12]
    with _lock, _connect() as conn:
        # 极小概率撞号时重试。
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
    return {
        "review_id": row["review_id"],
        "created_at": row["created_at"],
        "input": json.loads(row["payload"]),
        "conclusion": json.loads(row["conclusion"]),
    }
