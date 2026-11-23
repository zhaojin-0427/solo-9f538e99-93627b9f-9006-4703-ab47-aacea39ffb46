# -*- coding: utf-8 -*-
"""问卷跳题条件解析器与求值器。

条件语法（变量为问卷题号 qid）：

    谓词        Q1 == 'A'      单选题：答案是否等于选项 A
                Q2 != 'B'
                Q3 >= 3        数值题：> >= < <= == !=
                Q4 == true     是非题（右侧可写 true/false、'是'/'否'）
    布尔原子    Q5             等价于 Q5 == true（仅是非题）
    括号        ( ... )
    与          AND / && / 与 / 且
    或          OR / || / 或
    非          NOT / ! / 非
    常量        true / false（含中文 真/假、是/否、1/0）
    示例        Q1 == 'A' AND (Q3 >= 3 OR NOT Q2)

解析结果是纯数据 AST：
    ("var", qid)                 布尔原子
    ("cmp", qid, op, 字面量)     谓词，字面量为 str/int/float/bool
    ("not", 子式)
    ("and", 子式, ...)
    ("or",  子式, ...)
    ("true",) / ("false",)
"""
from __future__ import annotations

from typing import Any, List, Sequence, Tuple


class ConditionError(ValueError):
    """条件语法错误。"""


class EvalError(ValueError):
    """条件在某份答案上无法求值（如引用了未作答/被跳过的题）。"""


Token = Tuple[str, Any, int]  # (类型, 值, 起始位置)

_CMP_OPS = {"==", "!=", ">", ">=", "<", "<="}
_AND_WORDS = {"AND", "&&", "与", "且"}
_OR_WORDS = {"OR", "||", "或"}
_NOT_WORDS = {"NOT", "!", "非"}
_TRUE_WORDS = {"TRUE", "真", "YES", "是", "1"}
_FALSE_WORDS = {"FALSE", "假", "NO", "否", "0"}


def tokenize(text: str) -> List[Token]:
    tokens: List[Token] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "(":
            tokens.append(("LPAREN", "(", i))
            i += 1
            continue
        if ch == ")":
            tokens.append(("RPAREN", ")", i))
            i += 1
            continue
        if ch in "=!<>":
            j = i + 1
            if j < n and text[j] == "=":
                j += 1
            op = text[i:j]
            if op not in _CMP_OPS:
                raise ConditionError(f"无法识别的运算符 “{op}”（位置 {i + 1}）")
            tokens.append(("CMP", op, i))
            i = j
            continue
        if ch in "&|":
            j = i + 1
            if j < n and text[j] == ch:
                j += 1
            word = text[i:j]
            kind = "AND" if ch == "&" else "OR"
            tokens.append((kind, word, i))
            i = j
            continue
        if ch == "'" or ch == '"':
            quote = ch
            j = i + 1
            buf: List[str] = []
            while j < n and text[j] != quote:
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            if j >= n:
                raise ConditionError(f"字符串缺少结束引号（位置 {i + 1}）")
            tokens.append(("STRING", "".join(buf), i))
            i = j + 1
            continue
        if ch.isdigit() or (ch == "-" and i + 1 < n and text[i + 1].isdigit()):
            j = i + 1
            while j < n and (text[j].isdigit() or text[j] == "."):
                j += 1
            raw = text[i:j]
            try:
                num = float(raw) if "." in raw else int(raw)
            except ValueError as exc:
                raise ConditionError(f"非法数字 “{raw}”（位置 {i + 1}）") from exc
            tokens.append(("NUMBER", num, i))
            i = j
            continue
        if ch.isalpha() or ch == "_":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            word = text[i:j]
            up = word.upper()
            if up in _AND_WORDS:
                tokens.append(("AND", word, i))
            elif up in _OR_WORDS:
                tokens.append(("OR", word, i))
            elif up in _NOT_WORDS:
                tokens.append(("NOT", word, i))
            elif up in _TRUE_WORDS:
                tokens.append(("TRUE", word, i))
            elif up in _FALSE_WORDS:
                tokens.append(("FALSE", word, i))
            else:
                tokens.append(("IDENT", word, i))
            i = j
            continue
        raise ConditionError(f"无法识别的字符 “{ch}”（位置 {i + 1}）")
    return tokens


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.tokens = tokenize(text)
        self.pos = 0

    def peek(self) -> Token | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self) -> Token | None:
        tok = self.peek()
        if tok is not None:
            self.pos += 1
        return tok

    def expect(self, kind: str) -> Token:
        tok = self.next()
        if tok is None or tok[0] != kind:
            where = f"位置 {tok[2] + 1}" if tok else "条件末尾"
            actual = tok[1] if tok else "空"
            raise ConditionError(f"第 {where} 期望 {kind}，实际为 “{actual}”")
        return tok

    def parse(self) -> tuple:
        if not self.tokens:
            raise ConditionError("条件为空")
        node = self.parse_or()
        if self.peek() is not None:
            tok = self.peek()
            raise ConditionError(f"条件末尾有多余记号 “{tok[1]}”（位置 {tok[2] + 1}）")
        return node

    def parse_or(self) -> tuple:
        parts = [self.parse_and()]
        while self.peek() is not None and self.peek()[0] == "OR":
            self.next()
            parts.append(self.parse_and())
        return parts[0] if len(parts) == 1 else ("or",) + tuple(parts)

    def parse_and(self) -> tuple:
        parts = [self.parse_not()]
        while self.peek() is not None and self.peek()[0] == "AND":
            self.next()
            parts.append(self.parse_not())
        return parts[0] if len(parts) == 1 else ("and",) + tuple(parts)

    def parse_not(self) -> tuple:
        tok = self.peek()
        if tok is not None and tok[0] == "NOT":
            self.next()
            return ("not", self.parse_not())
        return self.parse_atom()

    def parse_atom(self) -> tuple:
        tok = self.next()
        if tok is None:
            raise ConditionError("条件不完整，缺少题号或括号表达式")
        kind, value, start = tok
        if kind == "LPAREN":
            node = self.parse_or()
            self.expect("RPAREN")
            return node
        if kind == "TRUE":
            return ("true",)
        if kind == "FALSE":
            return ("false",)
        if kind == "IDENT":
            nxt = self.peek()
            if nxt is not None and nxt[0] == "CMP":
                op = self.next()[1]
                lit_tok = self.next()
                if lit_tok is None or lit_tok[0] not in ("STRING", "NUMBER", "TRUE", "FALSE"):
                    where = f"位置 {lit_tok[2] + 1}" if lit_tok else "条件末尾"
                    raise ConditionError(f"第 {where} 比较运算符右侧必须是字面量")
                literal = self._literal(lit_tok)
                return ("cmp", value, op, literal)
            return ("var", value)
        raise ConditionError(f"意外的记号 “{value}”（位置 {start + 1}）")

    @staticmethod
    def _literal(tok: Token):
        kind, value, _ = tok
        if kind == "STRING":
            return value
        if kind == "NUMBER":
            return value
        return kind == "TRUE"


def parse_condition(text: str) -> tuple:
    """把条件文本解析为 AST。"""
    if not isinstance(text, str):
        raise ConditionError("条件必须是文本")
    return _Parser(text.strip()).parse()


def variables_in(node: Any) -> set:
    """收集 AST 中引用的全部题号。"""
    if not isinstance(node, tuple):
        return set()
    tag = node[0]
    if tag in ("var", "cmp"):
        return {node[1]}
    if tag == "not":
        return variables_in(node[1])
    if tag in ("and", "or"):
        out: set = set()
        for child in node[1:]:
            out |= variables_in(child)
        return out
    return set()


def _as_bool(value) -> bool:
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
    raise EvalError(f"值 {value!r} 不能解释为是非")


def _as_number(value):
    if isinstance(value, bool):
        raise EvalError(f"值 {value!r} 不是数值")
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value) if "." in value else int(value)
        except ValueError as exc:
            raise EvalError(f"值 {value!r} 不是数值") from exc
    raise EvalError(f"值 {value!r} 不是数值")


def evaluate(node: Any, answers: dict, questions: dict) -> bool:
    """在给定答案（qid -> 答案值）下求值。

    被引用的题未作答（通常意味着被跳过）时抛 EvalError，
    由相容性判定记为“展示/跳过记录与条件不符”。
    """
    tag = node[0]
    if tag == "true":
        return True
    if tag == "false":
        return False
    if tag == "not":
        return not evaluate(node[1], answers, questions)
    if tag == "and":
        return all(evaluate(c, answers, questions) for c in node[1:])
    if tag == "or":
        return any(evaluate(c, answers, questions) for c in node[1:])

    qid = node[1]
    if qid not in questions:
        raise EvalError(f"条件引用了不存在的题号 {qid}")
    if qid not in answers:
        raise EvalError(f"题 {qid} 未作答，无法据此判定展示条件")
    raw = answers[qid]
    question = questions[qid]

    if tag == "var":
        if question.qtype != "bool":
            raise EvalError(f"题 {qid} 不是是非题，不能直接作为布尔条件")
        return _as_bool(raw)

    # cmp
    op = node[2]
    literal = node[3]
    if question.qtype == "single":
        if op not in ("==", "!="):
            raise EvalError(f"单选题 {qid} 只支持 == / != 比较")
        actual = str(raw)
        return (actual == literal) if op == "==" else (actual != literal)
    if question.qtype == "number":
        actual = _as_number(raw)
        target = _as_number(literal)
        if op == "==":
            return actual == target
        if op == "!=":
            return actual != target
        if op == ">":
            return actual > target
        if op == ">=":
            return actual >= target
        if op == "<":
            return actual < target
        return actual <= target
    # bool
    if op not in ("==", "!="):
        raise EvalError(f"是非题 {qid} 只支持 == / != 比较")
    actual = _as_bool(raw)
    target = _as_bool(literal)
    return (actual == target) if op == "==" else (actual != target)


def ast_to_text(node: Any) -> str:
    """AST 转回可读文本（回显/调试用）。"""
    tag = node[0]
    if tag == "var":
        return node[1]
    if tag == "cmp":
        qid, op, literal = node[1], node[2], node[3]
        if isinstance(literal, str):
            lit = "'" + literal + "'"
        elif isinstance(literal, bool):
            lit = "true" if literal else "false"
        else:
            lit = str(literal)
        return f"{qid} {op} {lit}"
    if tag == "true":
        return "true"
    if tag == "false":
        return "false"
    if tag == "not":
        return "NOT " + ast_to_text(node[1])
    if tag == "and":
        return "(" + " AND ".join(ast_to_text(c) for c in node[1:]) + ")"
    if tag == "or":
        return "(" + " OR ".join(ast_to_text(c) for c in node[1:]) + ")"
    raise ConditionError(f"未知 AST 节点 {tag!r}")
