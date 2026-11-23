# -*- coding: utf-8 -*-
"""两版问卷配置的数据模型与校验。

单版配置结构（JSON）：
{
  "version": "v1",                       # 版号（任意非空字符串）
  "questions": [
    {"qid": "Q1", "qtype": "single", "options": ["A", "B", "C"],
     "condition": "Q0 == 'X'"},          # 展示条件；缺省/空表示无条件展示
    {"qid": "Q2", "qtype": "number", "min": 0, "max": 100},
    {"qid": "Q3", "qtype": "bool"},
    ...
  ]
}

题号在同一版内唯一；展示条件只能引用同版、且排在当前题之前的题号
（跳题只能依赖此前题目的作答）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .conditions import ConditionError, ast_to_text, parse_condition, variables_in

QTYPES = ("single", "number", "bool")


class ConfigError(ValueError):
    """配置不合法。"""


@dataclass(frozen=True)
class Question:
    qid: str
    qtype: str
    options: List[str] = field(default_factory=list)
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    condition_text: str = ""
    condition_ast: Optional[tuple] = None

    @property
    def has_condition(self) -> bool:
        return self.condition_ast is not None

    @property
    def condition_display(self) -> str:
        return ast_to_text(self.condition_ast) if self.condition_ast else "无条件（始终展示）"


@dataclass(frozen=True)
class VersionConfig:
    version: str
    questions: List[Question]

    @property
    def qids(self) -> List[str]:
        return [q.qid for q in self.questions]

    def index_of(self) -> Dict[str, int]:
        return {q.qid: i for i, q in enumerate(self.questions)}


@dataclass(frozen=True)
class QuestionnairePair:
    old: VersionConfig
    new: VersionConfig


def _norm_qid(value) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"题号必须是字符串，收到 {value!r}")
    qid = value.strip()
    if not qid:
        raise ConfigError("题号不能为空")
    return qid


def build_version_config(raw: Any, role: str = "配置") -> VersionConfig:
    """构建并完整校验一版问卷配置。role 用于错误信息（旧版/新版）。"""
    if not isinstance(raw, dict):
        raise ConfigError(f"{role}必须是 JSON 对象")
    version = raw.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ConfigError(f"{role}缺少非空字符串字段 version（版号）")
    version = version.strip()

    raw_questions = raw.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        raise ConfigError(f"{role}的 questions 必须是非空数组")

    questions: List[Question] = []
    seen: set = set()
    for idx, item in enumerate(raw_questions):
        where = f"{role}第 {idx + 1} 题"
        if not isinstance(item, dict):
            raise ConfigError(f"{where}必须是对象")
        qid = _norm_qid(item.get("qid"))
        if qid in seen:
            raise ConfigError(f"{where}：题号 {qid} 重复")
        seen.add(qid)
        qtype = item.get("qtype")
        if qtype not in QTYPES:
            raise ConfigError(f"{where}（{qid}）：qtype 必须是 { ' / '.join(QTYPES) } 之一")

        options: List[str] = []
        min_value = max_value = None
        if qtype == "single":
            raw_options = item.get("options")
            if not isinstance(raw_options, list) or not raw_options:
                raise ConfigError(f"{where}（{qid}）：单选题必须给出非空 options 数组")
            opt_seen: set = set()
            for opt in raw_options:
                if not isinstance(opt, str) or not opt.strip():
                    raise ConfigError(f"{where}（{qid}）：options 每一项必须是非空字符串")
                opt = opt.strip()
                if opt in opt_seen:
                    raise ConfigError(f"{where}（{qid}）：选项 {opt} 重复")
                opt_seen.add(opt)
                options.append(opt)
        elif qtype == "number":
            min_value = item.get("min")
            max_value = item.get("max")
            if min_value is not None:
                min_value = _as_number_literal(min_value, f"{where}（{qid}）的 min")
            if max_value is not None:
                max_value = _as_number_literal(max_value, f"{where}（{qid}）的 max")
            if min_value is not None and max_value is not None and min_value > max_value:
                raise ConfigError(f"{where}（{qid}）：min 不能大于 max")

        cond_text = (item.get("condition") or "").strip()
        cond_ast = None
        if cond_text:
            try:
                cond_ast = parse_condition(cond_text)
            except ConditionError as exc:
                raise ConfigError(f"{where}（{qid}）展示条件错误：{exc}") from exc
            refs = variables_in(cond_ast)
            earlier = {q.qid for q in questions}
            unknown = refs - earlier
            if unknown:
                raise ConfigError(
                    f"{where}（{qid}）展示条件引用了不存在或排在其后的题号："
                    + "、".join(sorted(unknown))
                )
        questions.append(
            Question(
                qid=qid,
                qtype=qtype,
                options=options,
                min_value=min_value,
                max_value=max_value,
                condition_text=cond_text,
                condition_ast=cond_ast,
            )
        )

    return VersionConfig(version=version, questions=questions)


def _as_number_literal(value, where: str):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where} 必须是数字")
    return value


def build_pair(raw_old: Any, raw_new: Any) -> QuestionnairePair:
    old_cfg = build_version_config(raw_old, "旧版配置")
    new_cfg = build_version_config(raw_new, "新版配置")
    if old_cfg.version == new_cfg.version:
        raise ConfigError("两版配置的 version 版号不能相同")
    return QuestionnairePair(old=old_cfg, new=new_cfg)


def version_to_jsonable(cfg: VersionConfig) -> dict:
    questions = []
    for q in cfg.questions:
        item: Dict[str, Any] = {"qid": q.qid, "qtype": q.qtype}
        if q.qtype == "single":
            item["options"] = list(q.options)
        if q.qtype == "number":
            if q.min_value is not None:
                item["min"] = q.min_value
            if q.max_value is not None:
                item["max"] = q.max_value
        if q.condition_text:
            item["condition"] = q.condition_text
        questions.append(item)
    return {"version": cfg.version, "questions": questions}
