# -*- coding: utf-8 -*-
"""版号核查引擎：两版配置相容判定 + 新版连续段划分（仅标准库）。

背景：一台调查终端试用新版夜骑问卷后换回旧版，导出的答卷没有版号。
给定旧版/新版两版配置（筛查项 + 各追问题展示条件），对每份答卷判定它与
各版本是否相容（已答项与逐题展示/跳过记录能否在该版本的跳题逻辑下同时
成立），再在流水号顺序上求“新版答卷恰好连成一段（可以为空）”的可行划分：

- 每份答卷得到 确定旧版 / 确定新版 / 待核 三态之一；
- 入仓只收“在所有可行划分下都属新版”的答卷；
- 整批无解时给出卡住划分的流水号及原因。

相容性用位掩码可行域判定：n 个筛查项（<=12）的全部 2^n 种真值组合编码为
一个大整数的低 2^n 位，每个追问题条件预编译为“条件为真的取值集合”位掩码；
一份答卷相容 <=> 已答项与各题展示/跳过记录对应的位掩码按位与之后非零。
单份判定只需常数次大整数运算，2000 份 × 2 版也可瞬间完成。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .conditions import ConditionError, ast_to_text, parse_condition
from .config import ConfigError, MAX_ITEMS, _as_bool

MAX_BATCH = 2000  # 每批答卷上限

# 三态
STATUS_OLD = "old"          # 确定旧版
STATUS_NEW = "new"          # 确定新版
STATUS_PENDING = "pending"  # 待核

STATUS_LABELS = {
    STATUS_OLD: "确定旧版",
    STATUS_NEW: "确定新版",
    STATUS_PENDING: "待核",
}


class BatchError(ValueError):
    """批次层面的问题（格式非法 / 超限 / 整批无解）。

    blockers：整批无解时卡住划分的逐条说明（含流水号），供页面展示。
    """

    def __init__(self, message: str, blockers: Optional[List[str]] = None):
        super().__init__(message)
        self.blockers: List[str] = list(blockers or [])


# ---------------------------------------------------------------------------
# 版本配置
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VersionFollowUp:
    qid: str
    condition_text: str
    condition_ast: tuple

    @property
    def condition_display(self) -> str:
        return ast_to_text(self.condition_ast)


@dataclass(frozen=True)
class VersionConfig:
    """一版问卷配置 + 预编译位掩码。

    full_mask：低 2^n 位全 1（全部取值组合都可行的初始可行域）。
    item_masks[name]：该筛查项取“是”的取值组合集合。
    cond_masks[qid]：该追问题展示条件为真的取值组合集合。
    """

    role: str                        # "old" / "new"
    items: List[str]
    labels: Dict[str, str]
    followups: List[VersionFollowUp]
    full_mask: int
    item_masks: Dict[str, int]
    cond_masks: Dict[str, int]

    def index_of(self) -> Dict[str, int]:
        return {name: i for i, name in enumerate(self.items)}

    @property
    def qids(self) -> List[str]:
        return [f.qid for f in self.followups]


def _item_true_mask(n: int, i: int) -> int:
    """第 i 个筛查项取“是”的全部取值组合位掩码（周期性的 0/1 块）。"""
    run = 1 << i                    # 连续 0 块 / 1 块的长度（按取值编号计）
    unit = ((1 << run) - 1) << run  # 一个周期：低 run 位为 0，高 run 位为 1
    mask = 0
    period = run * 2
    for r in range(1 << (n - i - 1)):
        mask |= unit << (r * period)
    return mask


def _ast_to_mask(node: tuple, item_masks: Dict[str, int], full: int) -> int:
    """把条件 AST 编译为“条件为真的取值组合”位掩码（大整数位运算）。"""
    tag = node[0]
    if tag == "var":
        return item_masks[node[1]]
    if tag == "true":
        return full
    if tag == "false":
        return 0
    if tag == "not":
        return full ^ _ast_to_mask(node[1], item_masks, full)
    if tag == "and":
        mask = full
        for child in node[1:]:
            mask &= _ast_to_mask(child, item_masks, full)
        return mask
    if tag == "or":
        mask = 0
        for child in node[1:]:
            mask |= _ast_to_mask(child, item_masks, full)
        return mask
    raise ConfigError(f"未知条件节点 {tag!r}")


def build_version_config(raw: dict, role: str) -> VersionConfig:
    """从可 JSON 序列化的 dict 构建一版问卷配置并预编译位掩码。

    raw 结构：
    {
      "items": ["S1", ...],                 # 至多 12 个
      "labels": {"S1": "夜间佩戴头盔"},      # 可选
      "followups": [{"qid": "Q1", "condition": "S1 AND NOT S2"}, ...]
    }
    role 为 "old" / "new"，仅用于报错信息。
    """
    where = "旧版配置" if role == "old" else "新版配置"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}必须是 JSON 对象")

    raw_items = raw.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ConfigError(f"{where} items 必须是非空数组")
    items: List[str] = []
    for it in raw_items:
        if not isinstance(it, str) or not it.strip():
            raise ConfigError(f"{where}的筛查项变量名必须是非空字符串，收到 {it!r}")
        key = it.strip()
        if key in items:
            raise ConfigError(f"{where}筛查项 {key} 重复定义")
        items.append(key)
    if len(items) > MAX_ITEMS:
        raise ConfigError(f"{where}筛查项最多 {MAX_ITEMS} 个，当前 {len(items)} 个")

    labels_raw = raw.get("labels", {}) or {}
    if not isinstance(labels_raw, dict):
        raise ConfigError(f"{where} labels 必须是对象")
    labels: Dict[str, str] = {}
    for k, v in labels_raw.items():
        key = str(k).strip()
        if key not in items:
            raise ConfigError(f"{where} labels 中的 {key} 不在 items 列表内")
        labels[key] = str(v)
    for name in items:
        labels.setdefault(name, name)

    followups_raw = raw.get("followups", [])
    if not isinstance(followups_raw, list):
        raise ConfigError(f"{where} followups 必须是数组")
    followups: List[VersionFollowUp] = []
    seen_qids: set = set()
    for f in followups_raw:
        if not isinstance(f, dict):
            raise ConfigError(f"{where} followups 每一项必须是对象")
        qid = str(f.get("qid", "")).strip()
        if not qid:
            raise ConfigError(f"{where}追问题号 qid 不能为空")
        if qid in seen_qids:
            raise ConfigError(f"{where}追问题号 {qid} 重复")
        seen_qids.add(qid)
        cond_text = f.get("condition", "")
        if not isinstance(cond_text, str) or not cond_text.strip():
            raise ConfigError(f"{where}追问题 {qid} 的展示条件为空")
        try:
            ast = parse_condition(cond_text, items)
        except ConditionError as exc:
            raise ConfigError(f"{where}追问题 {qid} 条件错误：{exc}") from exc
        followups.append(VersionFollowUp(qid, cond_text.strip(), ast))

    n = len(items)
    full_mask = (1 << (1 << n)) - 1
    item_masks = {name: _item_true_mask(n, i) for i, name in enumerate(items)}
    cond_masks = {
        f.qid: _ast_to_mask(f.condition_ast, item_masks, full_mask)
        for f in followups
    }
    return VersionConfig(
        role=role,
        items=items,
        labels=labels,
        followups=followups,
        full_mask=full_mask,
        item_masks=item_masks,
        cond_masks=cond_masks,
    )


# ---------------------------------------------------------------------------
# 答卷解析
# ---------------------------------------------------------------------------

_INT_RE = re.compile(r"^[+-]?\d+$")


def normalize_serial(value) -> str:
    """流水号归一化为字符串；只接受字符串或整数（布尔/小数/对象均拒绝）。"""
    if isinstance(value, bool):
        raise BatchError(f"流水号不能是布尔值：{value!r}")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise BatchError(f"流水号必须是字符串或整数，收到 {value!r}")
    if not text:
        raise BatchError("流水号不能为空")
    return text


def sort_serials(serials: Sequence[str]) -> List[str]:
    """流水号排序：整批都能按整数解释时按数值序，否则按字典序。"""
    if serials and all(_INT_RE.match(s) for s in serials):
        return sorted(serials, key=lambda s: (int(s), s))
    return sorted(serials)


@dataclass(frozen=True)
class ResponseRecord:
    """一份答卷：流水号 + 已答筛查项 + 逐题展示/跳过记录。"""

    serial: str
    answers: Dict[str, bool]
    shown: Dict[str, bool]

    def to_payload(self) -> dict:
        return {
            "serial": self.serial,
            "answers": dict(self.answers),
            "shown": dict(self.shown),
        }


def parse_response(raw, *, where: str = "答卷") -> ResponseRecord:
    """解析并校验一份答卷。

    结构：
    {
      "serial": "A0001",
      "answers": {"S1": true},                        # 可选，默认空
      "followups": [{"qid": "Q1", "shown": true}, ...]   # 与 shown 二选一
    }
    或 "shown": {"Q1": true, "Q2": false}
    逐题记录必须显式给出展示或跳过（followups 与 shown 同时出现视为歧义）。
    """
    if not isinstance(raw, dict):
        raise BatchError(f"{where}必须是 JSON 对象")
    if "serial" not in raw:
        raise BatchError(f"{where}缺少流水号 serial")
    try:
        serial = normalize_serial(raw["serial"])
    except BatchError as exc:
        raise BatchError(f"{where}：{exc}") from exc

    answers_raw = raw.get("answers", {})
    if not isinstance(answers_raw, dict):
        raise BatchError(f"{where} {serial}：answers 必须是对象")
    answers: Dict[str, bool] = {}
    for k, v in answers_raw.items():
        key = str(k).strip()
        if not key:
            raise BatchError(f"{where} {serial}：answers 中的筛查项名不能为空")
        if key in answers:
            raise BatchError(f"{where} {serial}：answers 中筛查项 {key} 重复")
        try:
            answers[key] = _as_bool(v, f"{where} {serial} 筛查项 {key} 的答案")
        except ConfigError as exc:
            raise BatchError(str(exc)) from exc

    has_list = "followups" in raw
    has_map = "shown" in raw
    if has_list and has_map:
        raise BatchError(f"{where} {serial}：followups 与 shown 只能二选一")
    if not has_list and not has_map:
        raise BatchError(
            f"{where} {serial}：缺少逐题展示/跳过记录（followups 或 shown）"
        )
    shown: Dict[str, bool] = {}
    if has_map:
        shown_raw = raw["shown"]
        if not isinstance(shown_raw, dict):
            raise BatchError(f"{where} {serial}：shown 必须是对象（题号 -> true/false）")
        entries = list(shown_raw.items())
    else:
        followups_raw = raw["followups"]
        if not isinstance(followups_raw, list):
            raise BatchError(f"{where} {serial}：followups 必须是数组")
        entries = []
        for entry in followups_raw:
            if not isinstance(entry, dict) or "qid" not in entry:
                raise BatchError(f"{where} {serial}：followups 每一项必须含 qid")
            if "shown" not in entry:
                raise BatchError(
                    f"{where} {serial}：追问题 {entry.get('qid')!r} 缺少 shown 记录"
                )
            entries.append((entry["qid"], entry["shown"]))
    for qid_raw, shown_raw in entries:
        qid = str(qid_raw).strip()
        if not qid:
            raise BatchError(f"{where} {serial}：追问题号不能为空")
        if qid in shown:
            raise BatchError(f"{where} {serial}：追问题号 {qid} 重复记录")
        try:
            shown[qid] = _as_bool(shown_raw, f"{where} {serial} 追问题 {qid} 的 shown")
        except ConfigError as exc:
            raise BatchError(str(exc)) from exc

    return ResponseRecord(serial=serial, answers=answers, shown=shown)


def parse_responses(raw) -> List[ResponseRecord]:
    """解析整批答卷，按流水号排序返回。上限 MAX_BATCH，流水号必须唯一。"""
    if isinstance(raw, dict) and "responses" in raw:
        raw_list = raw["responses"]
    else:
        raw_list = raw
    if not isinstance(raw_list, list):
        raise BatchError("答卷批次必须是数组，或含 responses 数组的对象")
    if not raw_list:
        raise BatchError("答卷批次为空，至少需要 1 份答卷")
    if len(raw_list) > MAX_BATCH:
        raise BatchError(
            f"每批最多 {MAX_BATCH} 份答卷，本次提交 {len(raw_list)} 份"
        )
    records: List[ResponseRecord] = []
    seen: set = set()
    for i, item in enumerate(raw_list, 1):
        rec = parse_response(item, where=f"第 {i} 份答卷")
        if rec.serial in seen:
            raise BatchError(f"流水号 {rec.serial} 在批次中重复")
        seen.add(rec.serial)
        records.append(rec)
    # 整批统一排序：全可按整数解释时按数值序，否则按字典序
    ordered = sort_serials([r.serial for r in records])
    by_serial = {r.serial: r for r in records}
    return [by_serial[s] for s in ordered]


# ---------------------------------------------------------------------------
# 相容判定
# ---------------------------------------------------------------------------

def judge(cfg: VersionConfig, rec: ResponseRecord) -> bool:
    """一份答卷是否与该版本相容。

    - 已答筛查项必须都在该版本的 items 内；
    - 逐题记录的题号集合必须恰好等于该版本的追问题集合
      （多记、少记都不符合该版本）；
    - 存在一组未答项补全，使每条展示/跳过记录与条件求值一致。
    """
    index = cfg.index_of()
    for name in rec.answers:
        if name not in index:
            return False
    if set(rec.shown) != set(cfg.cond_masks):
        return False
    mask = cfg.full_mask
    for name, value in rec.answers.items():
        item_mask = cfg.item_masks[name]
        mask &= item_mask if value else (cfg.full_mask ^ item_mask)
        if not mask:
            return False
    for qid, shown in rec.shown.items():
        cond_mask = cfg.cond_masks[qid]
        mask &= cond_mask if shown else (cfg.full_mask ^ cond_mask)
        if not mask:
            return False
    return True


# ---------------------------------------------------------------------------
# 连续段划分
# ---------------------------------------------------------------------------

@dataclass
class Classification:
    """整批划分结果。

    feasible=False 时 blockers 给出卡住划分的流水号说明，其余字段为空。
    feasible=True 时 statuses 覆盖全部流水号。
    """

    feasible: bool
    statuses: Dict[str, str] = field(default_factory=dict)
    blockers: List[str] = field(default_factory=list)
    warehouse: List[str] = field(default_factory=list)   # 确定新版（流水号序）
    must_new_span: Optional[Tuple[str, str]] = None      # 必属新版的流水号区间
    new_segment_bounds: Optional[Tuple[str, str]] = None  # 新版段最早起点/最晚终点
    counts: Dict[str, int] = field(default_factory=dict)


def classify(entries: Sequence[Tuple[str, bool, bool]]) -> Classification:
    """对按流水号排好序的 (serial, 可旧版, 可新版) 序列做连续段划分。

    约束：新版答卷在流水号顺序上恰好连成一段（可以为空段）。
    三态：
    - 确定新版：所有可行划分中都属新版（= 必属新版跨度内的全部答卷）；
    - 确定旧版：所有可行划分中都属旧版；
    - 待核：存在属新版与属旧版的可行划分各至少一种。
    """
    n = len(entries)
    serials = [e[0] for e in entries]
    old_ok = [e[1] for e in entries]
    new_ok = [e[2] for e in entries]

    neither = [i for i in range(n) if not old_ok[i] and not new_ok[i]]
    must_new = [i for i in range(n) if not old_ok[i] and new_ok[i]]
    must_old = [i for i in range(n) if old_ok[i] and not new_ok[i]]

    span_lo = must_new[0] if must_new else None
    span_hi = must_new[-1] if must_new else None
    inside_old: List[int] = []
    if must_new:
        inside_old = [i for i in must_old if span_lo <= i <= span_hi]

    if neither or inside_old:
        blockers: List[str] = []
        for i in neither:
            blockers.append(
                f"流水号 {serials[i]}：已答项与展示/跳过记录同旧版、新版均不相容"
            )
        if inside_old:
            a, b = serials[span_lo], serials[span_hi]
            for i in inside_old:
                blockers.append(
                    f"流水号 {serials[i]} 必属旧版，却夹在必属新版的 {a} 与 {b} "
                    f"之间，新版答卷无法连成一段"
                )
        return Classification(
            feasible=False,
            blockers=blockers,
            counts={"total": n, STATUS_OLD: 0, STATUS_NEW: 0, STATUS_PENDING: 0},
        )

    # 必属旧版位置的前缀计数，用于 O(1) 判断区间是否被旧版隔断
    blocked_prefix = [0] * (n + 1)
    for i in must_old:
        blocked_prefix[i + 1] = 1
    for i in range(n):
        blocked_prefix[i + 1] += blocked_prefix[i]

    def has_must_old_between(lo: int, hi: int) -> bool:
        return blocked_prefix[hi + 1] - blocked_prefix[lo] > 0

    statuses: Dict[str, str] = {}
    for i in range(n):
        if must_new and span_lo <= i <= span_hi:
            statuses[serials[i]] = STATUS_NEW      # 必属新版跨度内：任何划分都属新版
        elif not new_ok[i]:
            statuses[serials[i]] = STATUS_OLD      # 与新版不相容：只能属旧版
        elif not must_new:
            statuses[serials[i]] = STATUS_PENDING  # 新版段可为空，也可只覆盖自己
        else:
            cover_lo, cover_hi = min(i, span_lo), max(i, span_hi)
            # 存在覆盖自己且含必属新版跨度的连续段 <=> 之间没有必属旧版隔断
            statuses[serials[i]] = (
                STATUS_OLD
                if has_must_old_between(cover_lo, cover_hi)
                else STATUS_PENDING
            )

    warehouse = [s for s in serials if statuses[s] == STATUS_NEW]

    if must_new:
        start = 0
        end = n - 1
        for k in must_old:
            if k < span_lo:
                start = max(start, k + 1)
            elif k > span_hi:
                end = min(end, k - 1)
        bounds: Optional[Tuple[str, str]] = (serials[start], serials[end])
        span: Optional[Tuple[str, str]] = (serials[span_lo], serials[span_hi])
    else:
        bounds = None
        span = None

    counts = {
        "total": n,
        STATUS_NEW: sum(1 for s in statuses.values() if s == STATUS_NEW),
        STATUS_OLD: sum(1 for s in statuses.values() if s == STATUS_OLD),
        STATUS_PENDING: sum(1 for s in statuses.values() if s == STATUS_PENDING),
    }
    return Classification(
        feasible=True,
        statuses=statuses,
        warehouse=warehouse,
        must_new_span=span,
        new_segment_bounds=bounds,
        counts=counts,
    )
