# -*- coding: utf-8 -*-
"""答卷批次解析与版号可行划分求解。

核心事实（按流水号排序后的第 i 份答卷）：
- compat_old[i]：该答卷与旧版配置相容
- compat_new[i]：该答卷与新版配置相容

版号划分规则：新版答卷按流水号只能连成一段（也可以一段都没有），其余为旧版。
因此一个可行划分由区间 [l, r]（含端点，允许空区间）表示，区间内每份都必须
与新版相容，区间外每份都必须与旧版相容。

结论分类（取全部可行划分的交集）：
- 确定新版：在每个可行划分里都落在新版段
- 确定旧版：在每个可行划分里都落在新版段之外
- 待核    ：既有划分把它放新版、也有划分把它放旧版
- 无解    ：不存在任何可行划分，并给出卡住划分的流水号
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional

from .compat import check_compatibility
from .config import QuestionnairePair, VersionConfig

MAX_BATCH_SIZE = 2000

# 版号核查结论
CONFIRMED_OLD = "confirmed_old"
CONFIRMED_NEW = "confirmed_new"
PENDING = "pending"
UNSOLVABLE = "unsolvable"

VERDICT_LABELS = {
    CONFIRMED_OLD: "确定旧版",
    CONFIRMED_NEW: "确定新版",
    PENDING: "待核",
    UNSOLVABLE: "无解",
}


class BatchError(ValueError):
    """批次数据不合法（结构层面，区别于单份答卷的相容性）。"""


@dataclass
class ResponseData:
    """规范化后的单份答卷（原始数据保留在 raw，供更正与下载使用）。"""
    serial: str
    answers: Dict[str, Any]
    records: Dict[str, bool]
    raw: dict
    parse_error: Optional[str] = None
    # 相容性（parse_error 非空时均为 False）
    compat_old: bool = False
    compat_new: bool = False
    old_violations: List[str] = field(default_factory=list)
    new_violations: List[str] = field(default_factory=list)
    # 划分结论
    verdict: Optional[str] = None

    def copy_verdict_state(self) -> "ResponseData":
        """复制参与划分求解的可变状态（verdict），答案/相容标志等只读数据共享。

        更正试算时对副本列表求解，被拒绝也不会就地污染已提交快照里的对象。
        """
        return replace(
            self,
            answers=self.answers,
            records=self.records,
            raw=self.raw,
            old_violations=list(self.old_violations),
            new_violations=list(self.new_violations),
        )


@dataclass
class BatchResult:
    solvable: bool
    responses: List[ResponseData]
    stuck_serials: List[str] = field(default_factory=list)
    # 可行新版段的范围描述（用于页面说明）
    feasible_runs: List[Dict[str, int]] = field(default_factory=list)
    count_confirmed_new: int = 0
    count_confirmed_old: int = 0
    count_pending: int = 0

    @property
    def count(self) -> int:
        return len(self.responses)


# --------------------------------------------------------------------------
# 解析与规范化
# --------------------------------------------------------------------------

def _as_bool(value, where: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "真", "是"):
            return True
        if low in ("false", "0", "no", "假", "否"):
            return False
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise BatchError(f"{where} 必须是布尔值（true/false）")


def normalize_response(raw: Any) -> ResponseData:
    """把一份原始答卷 dict 规范化为 ResponseData；结构错误记录在 parse_error。"""
    if not isinstance(raw, dict):
        return ResponseData(serial="", answers={}, records={}, raw={},
                            parse_error="答卷不是 JSON 对象")
    serial = raw.get("serial")
    if not isinstance(serial, str) or not serial.strip():
        return ResponseData(serial="", answers=raw.get("answers") if isinstance(raw, dict) else {},
                            records={}, raw=raw, parse_error="缺少非空字符串字段 serial（流水号）")
    serial = serial.strip()

    raw_answers = raw.get("answers", {})
    if not isinstance(raw_answers, dict):
        return ResponseData(serial=serial, answers={}, records={}, raw=raw,
                            parse_error="answers 必须是 JSON 对象")
    answers: Dict[str, Any] = {}
    for k, v in raw_answers.items():
        if not isinstance(k, str) or not k.strip():
            return ResponseData(serial=serial, answers={}, records={}, raw=raw,
                                parse_error="answers 的题号必须是非空字符串")
        answers[k.strip()] = v

    records: Dict[str, bool] = {}
    if "records" in raw and raw["records"] is not None:
        raw_records = raw["records"]
        if not isinstance(raw_records, list):
            return ResponseData(serial=serial, answers=answers, records={}, raw=raw,
                                parse_error="records 必须是数组")
        for entry in raw_records:
            if not isinstance(entry, dict) or "qid" not in entry:
                return ResponseData(serial=serial, answers=answers, records={}, raw=raw,
                                    parse_error="records 每一项必须是含 qid 的对象")
            qid = str(entry["qid"]).strip()
            if not qid:
                return ResponseData(serial=serial, answers=answers, records={}, raw=raw,
                                    parse_error="records 中存在空题号")
            if qid in records:
                return ResponseData(serial=serial, answers=answers, records={}, raw=raw,
                                    parse_error=f"records 中题号 {qid} 重复")
            records[qid] = _as_bool(entry.get("shown", True), f"题 {qid} 的 shown")
    else:
        # 兼容形式：shown_qids（列出的为展示，其余为跳过，需结合版本题目全集核对）
        shown_qids = raw.get("shown_qids", [])
        if not isinstance(shown_qids, list):
            return ResponseData(serial=serial, answers=answers, records={}, raw=raw,
                                parse_error="shown_qids 必须是数组")
        for q in shown_qids:
            qid = str(q).strip()
            if not qid:
                return ResponseData(serial=serial, answers=answers, records={}, raw=raw,
                                    parse_error="shown_qids 中存在空题号")
            if qid in records:
                return ResponseData(serial=serial, answers=answers, records={}, raw=raw,
                                    parse_error=f"shown_qids 中题号 {qid} 重复")
            records[qid] = True

    return ResponseData(serial=serial, answers=answers, records=records, raw=raw)


def parse_batch(payload: Any, pair: QuestionnairePair) -> List[ResponseData]:
    """解析整批上传的 JSON：支持顶层数组或 {"responses": [...]}。"""
    if isinstance(payload, dict) and "responses" in payload:
        items = payload["responses"]
    else:
        items = payload
    if not isinstance(items, list):
        raise BatchError("答卷批次必须是数组，或含 responses 数组的对象")
    if not items:
        raise BatchError("答卷批次为空")
    if len(items) > MAX_BATCH_SIZE:
        raise BatchError(f"每批最多 {MAX_BATCH_SIZE} 份答卷，当前 {len(items)} 份")

    responses = [normalize_response(item) for item in items]
    serials = [r.serial for r in responses if r.serial]
    dup = sorted({s for s in serials if serials.count(s) > 1})
    if dup:
        raise BatchError("流水号重复：" + "、".join(dup))
    return responses


# --------------------------------------------------------------------------
# 相容性复算（更正一份时只调用这一份的逻辑）
# --------------------------------------------------------------------------

def recompute_compat(response: ResponseData, pair: QuestionnairePair) -> None:
    """就地重算单份答卷对两版的相容性。"""
    if response.parse_error is not None:
        response.compat_old = False
        response.compat_new = False
        response.old_violations = [response.parse_error]
        response.new_violations = [response.parse_error]
        return
    old_result = check_compatibility(
        {"answers": response.answers, "records": response.records}, pair.old
    )
    new_result = check_compatibility(
        {"answers": response.answers, "records": response.records}, pair.new
    )
    response.compat_old = old_result.compatible
    response.compat_new = new_result.compatible
    response.old_violations = old_result.violations
    response.new_violations = new_result.violations


# --------------------------------------------------------------------------
# 可行划分求解
# --------------------------------------------------------------------------

def _find_runs(flags: List[bool]) -> List[tuple]:
    """返回所有连续 True 段的 [起, 止]（含端点）。"""
    runs: List[tuple] = []
    start = None
    for i, ok in enumerate(flags):
        if ok and start is None:
            start = i
        elif not ok and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(flags) - 1))
    return runs


def solve(responses: List[ResponseData]) -> BatchResult:
    """按当前 compat_old/compat_new 求全部可行划分并给每份答卷下结论。

    会就地重写各 ResponseData 的 verdict。更正试算时调用方传入的是
    ResponseData.copy_verdict_state() 副本列表，因此试算无解被拒绝也不会
    污染已提交快照中的原始对象（见 engine/store.py）。
    """
    n = len(responses)
    compat_old = [r.compat_old for r in responses]
    compat_new = [r.compat_new for r in responses]

    # 结构损坏或与两版都不相容的答卷，直接卡住划分
    stuck = [
        responses[i].serial
        for i in range(n)
        if not compat_old[i] and not compat_new[i]
    ]

    # 必须进新版段（只与新版相容）/ 必须在新版段外（只与旧版相容）的位置
    must_new_idx = [i for i in range(n) if compat_new[i] and not compat_old[i]]
    must_old_idx = [i for i in range(n) if compat_old[i] and not compat_new[i]]
    both_idx = {i for i in range(n) if compat_old[i] and compat_new[i]}

    new_runs = _find_runs(compat_new)

    # 检查所有 must-new 位置能否被同一个新版相容段包住，且段内不含 must-old 点
    must_old_set = set(must_old_idx)
    chosen_run: Optional[tuple] = None
    if must_new_idx:
        first_mn, last_mn = must_new_idx[0], must_new_idx[-1]
        for run in new_runs:
            if run[0] <= first_mn and last_mn <= run[1]:
                if any(run[0] <= j <= run[1] for j in must_old_set):
                    continue
                chosen_run = run
                break
        if chosen_run is None and not stuck:
            # 找出最直接的卡住流水号：跨段的 must-new，或段内挡路的 must-old
            stuck = _explain_no_run(responses, must_new_idx, must_old_idx, new_runs)

    if stuck or (must_new_idx and chosen_run is None):
        if not stuck:
            stuck = _explain_no_run(responses, must_new_idx, must_old_idx, new_runs)
        for r in responses:
            r.verdict = UNSOLVABLE
        return BatchResult(
            solvable=False,
            responses=responses,
            stuck_serials=_dedupe_keep_order(stuck),
            feasible_runs=[],
        )

    verdicts = [PENDING] * n
    feasible_runs: List[Dict[str, int]] = []

    if chosen_run is not None:
        # 可行新版段为 chosen_run 的任意含 [first_mn, last_mn] 的子区间
        first_mn, last_mn = must_new_idx[0], must_new_idx[-1]
        run_start, run_end = chosen_run
        for i in range(n):
            if first_mn <= i <= last_mn:
                verdicts[i] = CONFIRMED_NEW
            elif run_start <= i <= run_end and i in both_idx:
                verdicts[i] = PENDING
            else:
                verdicts[i] = CONFIRMED_OLD
        feasible_runs = [{
            "run_start": run_start,
            "run_end": run_end,
            "must_start": first_mn,
            "must_end": last_mn,
        }]
    else:
        # 没有 must-new 点：空段（全旧）或任一新版相容段的任意子区间都是可行划分
        for i in range(n):
            verdicts[i] = PENDING if i in both_idx else CONFIRMED_OLD
        feasible_runs = [
            {"run_start": s, "run_end": e, "must_start": -1, "must_end": -1}
            for (s, e) in new_runs
        ]

    for r, v in zip(responses, verdicts):
        r.verdict = v

    return BatchResult(
        solvable=True,
        responses=responses,
        stuck_serials=[],
        feasible_runs=feasible_runs,
        count_confirmed_new=sum(v == CONFIRMED_NEW for v in verdicts),
        count_confirmed_old=sum(v == CONFIRMED_OLD for v in verdicts),
        count_pending=sum(v == PENDING for v in verdicts),
    )


def _explain_no_run(responses, must_new_idx, must_old_idx, new_runs) -> List[str]:
    """无解（非两版皆不相容）时挑出卡住划分的流水号。"""
    first_mn, last_mn = must_new_idx[0], must_new_idx[-1]
    # 落在首个/末个必须新版位置之间、却与新版不相容的点
    blockers = [
        responses[i].serial
        for i in range(first_mn, last_mn + 1)
        if not responses[i].compat_new
    ]
    if blockers:
        return blockers
    # 否则是 must-new 点分散在多个新版相容段
    covering_runs = [
        run for run in new_runs
        if any(run[0] <= i <= run[1] for i in must_new_idx)
    ]
    if len(covering_runs) >= 2:
        return [responses[first_mn].serial, responses[last_mn].serial]
    # 兜底：段内挡路的 must-old 点
    return [responses[i].serial for i in must_old_idx] or [
        responses[first_mn].serial
    ]


def _dedupe_keep_order(items: List[str]) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def build_batch(payload: Any, pair: QuestionnairePair) -> BatchResult:
    """解析 + 逐份相容 + 求解 的完整入口。"""
    responses = parse_batch(payload, pair)
    for r in responses:
        recompute_compat(r, pair)
    return solve(responses)
