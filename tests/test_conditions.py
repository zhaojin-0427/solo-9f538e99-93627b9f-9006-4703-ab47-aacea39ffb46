# -*- coding: utf-8 -*-
"""条件解析与求值测试。"""
import unittest

from app.engine.conditions import (
    ConditionError,
    EvalError,
    ast_to_text,
    evaluate,
    parse_condition,
    variables_in,
)
from app.engine.config import Question


def q(qid, qtype="bool", **kw):
    return Question(qid=qid, qtype=qtype, **kw)


class ConditionTests(unittest.TestCase):
    def test_parse_cmp_and_bool_atom(self):
        node = parse_condition("Q1 == 'A' AND Q3 >= 3 OR NOT Q2")
        self.assertEqual(node[0], "or")
        self.assertEqual(variables_in(node), {"Q1", "Q2", "Q3"})

    def test_chinese_operators(self):
        node = parse_condition("非Q2 与 (Q1 == 'A' 或 Q3 > 10)")
        self.assertEqual(node[0], "and")

    def test_syntax_error(self):
        with self.assertRaises(ConditionError):
            parse_condition("Q1 ==")
        with self.assertRaises(ConditionError):
            parse_condition("Q1 == 'A' AND")

    def test_eval_single_question(self):
        questions = {"Q1": q("Q1", "single", options=["A", "B"])}
        node = parse_condition("Q1 == 'A'")
        self.assertTrue(evaluate(node, {"Q1": "A"}, questions))
        self.assertFalse(evaluate(parse_condition("Q1 != 'A'"), {"Q1": "A"}, questions))

    def test_eval_number_question(self):
        questions = {"Q3": q("Q3", "number", min_value=0, max_value=100)}
        self.assertTrue(evaluate(parse_condition("Q3 >= 30"), {"Q3": 40}, questions))
        self.assertFalse(evaluate(parse_condition("Q3 >= 30"), {"Q3": 25}, questions))
        self.assertTrue(evaluate(parse_condition("Q3 < 60"), {"Q3": "59"}, questions))

    def test_eval_bool_atom_and_constants(self):
        questions = {"Q2": q("Q2", "bool")}
        self.assertTrue(evaluate(parse_condition("Q2"), {"Q2": True}, questions))
        self.assertTrue(evaluate(parse_condition("Q2 == true"), {"Q2": "是"}, questions))
        self.assertFalse(evaluate(parse_condition("Q2"), {"Q2": "否"}, questions))

    def test_eval_missing_answer_raises(self):
        questions = {"Q1": q("Q1", "bool"), "Q2": q("Q2", "bool")}
        with self.assertRaises(EvalError):
            evaluate(parse_condition("Q1 AND Q2"), {"Q1": True}, questions)

    def test_ast_to_text_roundtrip(self):
        text = "Q1 == 'A' AND (Q3 >= 30 OR NOT Q2)"
        self.assertEqual(ast_to_text(parse_condition(text)),
                         "(Q1 == 'A' AND (Q3 >= 30 OR NOT Q2))")


if __name__ == "__main__":
    unittest.main()
