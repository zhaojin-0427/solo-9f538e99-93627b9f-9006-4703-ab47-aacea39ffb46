# -*- coding: utf-8 -*-
"""版号核查批次存储：SQLite 单文件，事务化提交，下载走一致性快照。

设计要点：
- 批次与答卷存于 SQLite（WAL 模式），gunicorn 多 worker 共享同一文件；
- 每次成功导入/更正构成一个“已提交状态”，state_version 单调递增；
- 导入或更正若使整批无解：事务回滚，原数据与下载结果照旧，并报出
  卡住划分的流水号；
- 更正只重判被更正那一份与两版是否相容（2 次位掩码判定），其余答卷的
  相容位原样保留、不改写；可行性用 MIN/MAX/COUNT 等聚合查询在索引上完成，
  不逐份扫描；
- 下载在单个读事务内取数，保证文件里每份答卷与版号来自同一个已提交状态。
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .engine.versioncheck import (
    BatchError,
    Classification,
    ResponseRecord,
    VersionConfig,
    build_version_config,
    classify,
    judge,
    parse_response,
    parse_responses,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
  id            TEXT PRIMARY KEY,
  created_at    TEXT NOT NULL,
  state_version INTEGER NOT NULL,
  old_config    TEXT NOT NULL,
  new_config    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS responses (
  batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
  serial   TEXT NOT NULL,
  ord      INTEGER NOT NULL,
  payload  TEXT NOT NULL,
  old_ok   INTEGER NOT NULL,
  new_ok   INTEGER NOT NULL,
  PRIMARY KEY (batch_id, serial)
);
CREATE INDEX IF NOT EXISTS idx_responses_ord ON responses(batch_id, ord);
"""


class BatchNotFound(KeyError):
    """会话指向的批次不存在（已重置或服务重启后库已清空）。"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str) -> None:
    directory = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(directory, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def _canonical_payload(rec: ResponseRecord) -> str:
    return json.dumps(rec.to_payload(), ensure_ascii=False, sort_keys=True)


def _load_configs(row: sqlite3.Row) -> Tuple[VersionConfig, VersionConfig]:
    old_cfg = build_version_config(json.loads(row["old_config"]), "old")
    new_cfg = build_version_config(json.loads(row["new_config"]), "new")
    return old_cfg, new_cfg


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------

def create_batch(db_path: str, old_raw: dict, new_raw: dict, responses_raw
                 ) -> Tuple[str, Classification]:
    """校验并导入整批答卷；整批无解则不入库并抛出带流水号的 BatchError。"""
    old_cfg = build_version_config(old_raw, "old")
    new_cfg = build_version_config(new_raw, "new")
    records = parse_responses(responses_raw)

    entries = [
        (rec.serial, judge(old_cfg, rec), judge(new_cfg, rec))
        for rec in records
    ]
    result = classify(entries)
    if not result.feasible:
        raise BatchError(
            f"整批无解：{len(result.blockers)} 处记录卡住划分，批次未导入",
            result.blockers,
        )

    batch_id = uuid.uuid4().hex
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO batches(id, created_at, state_version, old_config, new_config)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                batch_id,
                _utcnow(),
                1,
                json.dumps(old_raw, ensure_ascii=False, sort_keys=True),
                json.dumps(new_raw, ensure_ascii=False, sort_keys=True),
            ),
        )
        conn.executemany(
            "INSERT INTO responses(batch_id, serial, ord, payload, old_ok, new_ok)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    batch_id,
                    rec.serial,
                    position,
                    _canonical_payload(rec),
                    int(old_ok),
                    int(new_ok),
                )
                for position, (rec, (_, old_ok, new_ok))
                in enumerate(zip(records, entries))
            ],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return batch_id, result


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

@dataclass
class ResponseView:
    serial: str
    status: str
    old_ok: bool
    new_ok: bool
    answers: Dict[str, bool]
    shown: Dict[str, bool]


@dataclass
class BatchView:
    batch_id: str
    state_version: int
    created_at: str
    old_config: dict
    new_config: dict
    rows: List[ResponseView]
    classification: Classification

    def detail(self, serial: str) -> Optional[ResponseView]:
        for row in self.rows:
            if row.serial == serial:
                return row
        return None


def _fetch_batch_row(conn: sqlite3.Connection, batch_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM batches WHERE id = ?", (batch_id,)
    ).fetchone()
    if row is None:
        raise BatchNotFound(batch_id)
    return row


def load_view(db_path: str, batch_id: str) -> BatchView:
    """读取批次当前已提交状态并重新分类（只读，不做任何相容重判）。"""
    conn = connect(db_path)
    try:
        batch = _fetch_batch_row(conn, batch_id)
        rows = conn.execute(
            "SELECT serial, payload, old_ok, new_ok FROM responses"
            " WHERE batch_id = ? ORDER BY ord",
            (batch_id,),
        ).fetchall()
    finally:
        conn.close()

    entries = [(r["serial"], bool(r["old_ok"]), bool(r["new_ok"])) for r in rows]
    result = classify(entries)
    views = []
    for r in rows:
        payload = json.loads(r["payload"])
        views.append(
            ResponseView(
                serial=r["serial"],
                status=result.statuses[r["serial"]],
                old_ok=bool(r["old_ok"]),
                new_ok=bool(r["new_ok"]),
                answers=payload["answers"],
                shown=payload["shown"],
            )
        )
    return BatchView(
        batch_id=batch_id,
        state_version=batch["state_version"],
        created_at=batch["created_at"],
        old_config=json.loads(batch["old_config"]),
        new_config=json.loads(batch["new_config"]),
        rows=views,
        classification=result,
    )


# ---------------------------------------------------------------------------
# 更正（单份）
# ---------------------------------------------------------------------------

def correct_response(db_path: str, batch_id: str, payload_raw
                     ) -> Tuple[ResponseRecord, Classification, int]:
    """按流水号更正一份答卷。

    只重判这一份与两版是否相容；其余答卷的相容位与内容不改写、不逐份
    扫描。若更正使整批无解，事务回滚并抛出带卡住流水号的 BatchError，
    原数据与下载结果照旧。成功则提交为新状态（state_version + 1）。
    """
    rec = parse_response(payload_raw, where="更正答卷")

    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        batch = _fetch_batch_row(conn, batch_id)
        row = conn.execute(
            "SELECT serial, ord, payload, old_ok, new_ok FROM responses"
            " WHERE batch_id = ? AND serial = ?",
            (batch_id, rec.serial),
        ).fetchone()
        if row is None:
            conn.rollback()
            raise BatchError(f"流水号 {rec.serial} 不在当前批次内，无法更正")

        old_cfg, new_cfg = _load_configs(batch)
        old_ok = judge(old_cfg, rec)   # 只重判这一份
        new_ok = judge(new_cfg, rec)
        ord_self = row["ord"]

        # —— 用聚合查询（索引上的 MIN/MAX/点查）做可行性检查，不逐份扫描 ——
        args = (batch_id, rec.serial)
        neither_serials = [
            r[0]
            for r in conn.execute(
                "SELECT serial FROM responses"
                " WHERE batch_id = ? AND serial <> ? AND old_ok = 0 AND new_ok = 0",
                args,
            )
        ]
        if not old_ok and not new_ok:
            neither_serials.append(rec.serial)

        blockers: List[str] = []
        for s in sorted(neither_serials):
            blockers.append(
                f"流水号 {s}：已答项与展示/跳过记录同旧版、新版均不相容"
            )

        if not neither_serials:
            span = conn.execute(
                "SELECT MIN(ord) AS lo, MAX(ord) AS hi FROM responses"
                " WHERE batch_id = ? AND serial <> ? AND old_ok = 0 AND new_ok = 1",
                args,
            ).fetchone()
            lo, hi = span["lo"], span["hi"]
            if not old_ok and new_ok:  # 被更正的这份变为必属新版
                lo = ord_self if lo is None else min(lo, ord_self)
                hi = ord_self if hi is None else max(hi, ord_self)
            if lo is not None:
                inside = [
                    r[0]
                    for r in conn.execute(
                        "SELECT serial FROM responses"
                        " WHERE batch_id = ? AND serial <> ?"
                        "   AND old_ok = 1 AND new_ok = 0 AND ord BETWEEN ? AND ?",
                        (batch_id, rec.serial, lo, hi),
                    )
                ]
                if old_ok and not new_ok and lo <= ord_self <= hi:
                    inside.append(rec.serial)
                if inside:
                    serial_lo = rec.serial if lo == ord_self else conn.execute(
                        "SELECT serial FROM responses WHERE batch_id = ? AND ord = ?",
                        (batch_id, lo),
                    ).fetchone()[0]
                    serial_hi = rec.serial if hi == ord_self else conn.execute(
                        "SELECT serial FROM responses WHERE batch_id = ? AND ord = ?",
                        (batch_id, hi),
                    ).fetchone()[0]
                    for s in sorted(inside):
                        blockers.append(
                            f"流水号 {s} 必属旧版，却夹在必属新版的 {serial_lo} "
                            f"与 {serial_hi} 之间，新版答卷无法连成一段"
                        )

        if blockers:
            conn.rollback()
            raise BatchError(
                f"更正使整批无解，已回退到更正前状态：{len(blockers)} 处记录卡住划分",
                blockers,
            )

        new_payload = _canonical_payload(rec)
        if (new_payload == row["payload"]
                and int(old_ok) == row["old_ok"] and int(new_ok) == row["new_ok"]):
            conn.rollback()  # 内容无变化，不产生新状态
            view = load_view(db_path, batch_id)
            return rec, view.classification, view.state_version

        conn.execute(
            "UPDATE responses SET payload = ?, old_ok = ?, new_ok = ?"
            " WHERE batch_id = ? AND serial = ?",
            (new_payload, int(old_ok), int(new_ok), batch_id, rec.serial),
        )
        conn.execute(
            "UPDATE batches SET state_version = state_version + 1 WHERE id = ?",
            (batch_id,),
        )
        conn.commit()
    except (BatchError, BatchNotFound):
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    view = load_view(db_path, batch_id)
    return rec, view.classification, view.state_version


# ---------------------------------------------------------------------------
# 入仓下载（一致性快照）
# ---------------------------------------------------------------------------

def warehouse_snapshot(db_path: str, batch_id: str
                       ) -> Tuple[int, str, List[dict]]:
    """取入仓答卷：仅收在所有可行划分下都属新版的答卷。

    整个读取在单个读事务内完成，返回的 (state_version, entries) 保证来自
    同一个已提交状态；更正事务提交前，下载看到的仍是上一个状态。
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN")  # 读事务：本次快照不受并发更正影响
        batch = _fetch_batch_row(conn, batch_id)
        rows = conn.execute(
            "SELECT serial, payload, old_ok, new_ok FROM responses"
            " WHERE batch_id = ? ORDER BY ord",
            (batch_id,),
        ).fetchall()
        state_version = batch["state_version"]
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    entries = [(r["serial"], bool(r["old_ok"]), bool(r["new_ok"])) for r in rows]
    result = classify(entries)
    payloads = {r["serial"]: json.loads(r["payload"]) for r in rows}
    out = []
    for serial in result.warehouse:
        payload = payloads[serial]
        out.append(
            {
                "serial": serial,
                "version": "new",
                "answers": payload["answers"],
                "shown": payload["shown"],
            }
        )
    return state_version, _utcnow(), out


def drop_batch(db_path: str, batch_id: str) -> None:
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM responses WHERE batch_id = ?", (batch_id,))
        conn.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
