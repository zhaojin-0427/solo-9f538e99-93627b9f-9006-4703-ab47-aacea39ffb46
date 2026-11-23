# -*- coding: utf-8 -*-
"""单份答卷与某一版问卷配置的相容性判定。

相容的含义（全部满足）：
1. 答卷逐题展示/跳过记录覆盖该版本的每一道题，且不出现该版本没有的题号；
2. 对每道题，按该版本的展示条件（仅依赖此前题目的已答项）推出的展示/跳过
   与记录一致；条件引用到未作答题目时视为应跳过（前置题被跳，后续必跳）；
3. 展示的题必须有已答项，跳过的题必须没有已答项；
4. 已答项的值符合题型（单选在 options 内、数值在 min/max 内、是非可解释为布尔）。

判定纯函数化：给定答卷与版本配置即可复算，更正时只需要重算这一份。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .conditions import EvalError, evaluate
from .config import Question, VersionConfig


@dataclass
class CompatResult:
    compatible: bool
    violations: List[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "相容" if self.compatible else "；".join(self.violations)


def _coerce_bool(value, qid: str, violations: List[str]) -> Optional[bool]:
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
    violations.append(f"题 {qid} 的答案 {value!r} 不能解释为是非值")
    return None


def _coerce_number(value, question: Question, violations: List[str]):
    if isinstance(value, bool):
        violations.append(f"题 {question.qid} 的答案 {value!r} 不是数值")
        return None
    if isinstance(value, (int, float)):
        num = value
    elif isinstance(value, str):
        try:
            num = float(value) if "." in value else int(value)
        except ValueError:
            violations.append(f"题 {question.qid} 的答案 {value!r} 不是数值")
            return None
    else:
        violations.append(f"题 {question.qid} 的答案 {value!r} 不是数值")
        return None
    if question.min_value is not None and num < question.min_value:
        violations.append(f"题 {question.qid} 的答案 {num} 小于下限 {question.min_value}")
    if question.max_value is not None and num > question.max_value:
        violations.append(f"题 {question.qid} 的答案 {num} 大于上限 {question.max_value}")
    return num


def check_answer_value(question: Question, value: Any, violations: List[str]) -> None:
    qid = question.qid
    if question.qtype == "bool":
        _coerce_bool(value, qid, violations)
    elif question.qtype == "number":
        _coerce_number(value, question, violations)
    else:  # single
        if not isinstance(value, str):
            violations.append(f"题 {qid} 的答案 {value!r} 不是字符串选项")
        elif value not in question.options:
            violations.append(
                f"题 {qid} 的答案 {value!r} 不在选项 {question.options} 内"
            )


def check_compatibility(response: dict, cfg: VersionConfig) -> CompatResult:
    """判定一份答卷是否与给定版本相容。response 为规范化后的答卷 dict。

    版本外题号的处理遵循“宽出严进”：
    - 答卷带有该版本不存在的题号（已答项或展示/跳过记录）=> 不相容，
      例如旧版配置只有 4 题，答卷却出现新版独有题 Q5；
    - 答卷未覆盖该版本的题目 => 视为该题未展示（跳过），继续按条件核对，
      例如旧版答卷没有新版 Q5 的记录，并不影响它与旧版的相容性。
    """
    violations: List[str] = []
    answers = response.get("answers", {})
    records = response.get("records", {})

    questions: Dict[str, Question] = {q.qid: q for q in cfg.questions}

    # 1a. 答卷不能出现该版本之外的题号（在更新版本上作答却挂到旧版 => 不相容）
    unknown_record_qids = sorted(set(records) - set(questions))
    if unknown_record_qids:
        violations.append(
            "展示/跳过记录含该版本不存在的题号：" + "、".join(unknown_record_qids)
        )
    unknown_answer_qids = sorted(set(answers) - set(questions))
    if unknown_answer_qids:
        violations.append(
            "已答项含该版本不存在的题号：" + "、".join(unknown_answer_qids)
        )

    # 1b. 未列出的题目按“未展示（跳过）”处理（旧版答卷天然缺少新版独有题的记录）
    def record_of(qid: str) -> bool:
        return bool(records.get(qid, False))

    # 2+3. 逐题核对展示条件、记录与已答项
    for question in cfg.questions:
        qid = question.qid
        shown_record = record_of(qid)

        if question.has_condition:
            try:
                expect_shown = evaluate(question.condition_ast, answers, questions)
            except EvalError:
                # 前置题未作答（被跳过）→ 本题也应当被跳过
                expect_shown = False
        else:
            expect_shown = True

        if shown_record != expect_shown:
            verb = "展示" if shown_record else "跳过"
            expect_verb = "展示" if expect_shown else "跳过"
            violations.append(
                f"题 {qid} 记录为{verb}，但按该版本展示条件（{question.condition_display}）应{expect_verb}"
            )
            continue

        answered = qid in answers
        if shown_record and not answered:
            violations.append(f"题 {qid} 记录为展示，但缺少已答项")
        elif not shown_record and answered:
            violations.append(f"题 {qid} 记录为跳过，却出现了已答项 {answers[qid]!r}")
        elif shown_record and answered:
            check_answer_value(question, answers[qid], violations)

    return CompatResult(compatible=not violations, violations=violations)
