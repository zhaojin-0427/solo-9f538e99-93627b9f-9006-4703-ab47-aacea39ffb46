# -*- coding: utf-8 -*-
"""批次状态存储：带锁、带版本号（revision）的原子提交内存状态。

设计要点：
- 每次导入或成功更正都生成新的不可变快照（Snapshot），revision 单调递增；
- 更正只重算被更正的那一份答卷的相容性，再跑一次纯 O(n) 的区间求解，
  不逐份扫描/改写其余答卷的数据（它们的答案、记录与相容结论对象原样保留）；
- 更正导致整批无解时拒绝提交，旧快照与既有下载结果保持不变；
- 下载从当前已提交快照取数，更正进行中（锁内）无法插入下载，
  因而一次下载内的每份答卷与其版号必然来自同一个已提交状态。
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .config import QuestionnairePair
from .solver import (
    BatchResult,
    CONFIRMED_NEW,
    ResponseData,
    VERDICT_LABELS,
    build_batch,
    normalize_response,
    recompute_compat,
    solve,
)


class StoreError(ValueError):
    """更正无法提交。"""


@dataclass(frozen=True)
class Snapshot:
    revision: int
    pair: QuestionnairePair
    responses: tuple          # ResponseData 元组（提交后不再就地修改）
    batch: BatchResult
    created_at: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class BatchStore:
    def __init__(self):
        self._lock = threading.RLock()
        self._snapshot: Optional[Snapshot] = None
        self._pair_only: Optional[QuestionnairePair] = None

    # ------------------------------------------------------------------
    def has_snapshot(self) -> bool:
        with self._lock:
            return self._snapshot is not None

    def has_pair(self) -> bool:
        with self._lock:
            return self._snapshot is not None or self._pair_only is not None

    def get(self) -> Optional[Snapshot]:
        with self._lock:
            return self._snapshot

    def get_pair(self) -> Optional[QuestionnairePair]:
        with self._lock:
            return self._snapshot.pair if self._snapshot is not None else self._pair_only

    def set_pair_only(self, pair: QuestionnairePair) -> None:
        """只登记两版配置，尚未导入答卷。"""
        with self._lock:
            self._pair_only = pair
            self._snapshot = None

    def clear(self) -> None:
        with self._lock:
            self._snapshot = None
            self._pair_only = None

    # ------------------------------------------------------------------
    def load_batch(self, payload: Any, pair: QuestionnairePair) -> Snapshot:
        """导入新批次（替换当前状态）。结构错误向上抛 BatchError。"""
        batch = build_batch(payload, pair)
        with self._lock:
            snap = Snapshot(
                revision=1,
                pair=pair,
                responses=tuple(batch.responses),
                batch=batch,
                created_at=_now_iso(),
            )
            self._snapshot = snap
            self._pair_only = None
            return snap

    # ------------------------------------------------------------------
    def correct(self, serial: str, corrected_raw: Any) -> Snapshot:
        """按流水号更正一份答卷。

        只规范化并重算这一份与两版的相容性，其余答卷对象原样保留；
        若更正后整批无解，抛 StoreError 且当前快照不变。
        """
        with self._lock:
            if self._snapshot is None:
                raise StoreError("尚未导入任何批次")
            current = self._snapshot
            responses: List[ResponseData] = list(current.responses)
            target_pos = next(
                (i for i, r in enumerate(responses) if r.serial == serial), None
            )
            if target_pos is None:
                raise StoreError(f"流水号 {serial} 不在当前批次内")

            # 仅构造并复算这一份，不触碰其余答卷
            new_item = normalize_response(corrected_raw)
            if new_item.parse_error is not None:
                raise StoreError(f"更正数据不合法：{new_item.parse_error}")
            if new_item.serial != serial:
                raise StoreError(
                    f"更正数据的流水号 {new_item.serial!r} 与被更正流水号 {serial!r} 不一致"
                )
            recompute_compat(new_item, current.pair)

            # 全部用 verdict 状态副本试算：即使无解也不会污染已提交快照中的对象
            trial_responses = [
                r.copy_verdict_state() if i != target_pos else new_item
                for i, r in enumerate(responses)
            ]
            trial_batch = solve(trial_responses)
            if not trial_batch.solvable:
                stuck = "、".join(trial_batch.stuck_serials) or serial
                raise StoreError(
                    f"更正会使整批无解，已拒绝；卡住划分的流水号：{stuck}。"
                    "原数据与下载结果保持不变。"
                )

            snap = Snapshot(
                revision=current.revision + 1,
                pair=current.pair,
                responses=tuple(trial_batch.responses),
                batch=trial_batch,
                created_at=_now_iso(),
            )
            self._snapshot = snap
            return snap

    # ------------------------------------------------------------------
    def export_payload(self) -> dict:
        """构造入仓 JSON：只收在所有可行划分下都属新版的答卷（确定新版）。"""
        with self._lock:
            snap = self._snapshot
            if snap is None:
                raise StoreError("尚未导入任何批次")
            pair = snap.pair
            accepted = [
                r for r in snap.responses if r.verdict == CONFIRMED_NEW
            ]
            return {
                "exported_at": _now_iso(),
                "source_revision": snap.revision,
                "new_version": pair.new.version,
                "old_version": pair.old.version,
                "rule": "仅收录在所有可行版号划分下都属于新版的答卷",
                "total_in_batch": len(snap.responses),
                "accepted_count": len(accepted),
                "responses": [_response_to_jsonable(r) for r in accepted],
            }


def _response_to_jsonable(r: ResponseData) -> dict:
    return {
        "serial": r.serial,
        "answers": r.answers,
        "records": [
            {"qid": qid, "shown": shown}
            for qid, shown in sorted(r.records.items())
        ],
        "verdict": r.verdict,
        "verdict_label": VERDICT_LABELS.get(r.verdict, r.verdict),
    }


def dump_snapshot_meta(snap: Snapshot) -> dict:
    """供页面/调试展示的快照元信息。"""
    return {
        "revision": snap.revision,
        "created_at": snap.created_at,
        "solvable": snap.batch.solvable,
        "total": snap.batch.count,
        "confirmed_new": snap.batch.count_confirmed_new,
        "confirmed_old": snap.batch.count_confirmed_old,
        "pending": snap.batch.count_pending,
        "stuck_serials": snap.batch.stuck_serials,
        "old_version": snap.pair.old.version,
        "new_version": snap.pair.new.version,
    }


# 进程级单例（gunicorn 单 worker + 多线程；线程安全由内部锁保证）
batch_store = BatchStore()
