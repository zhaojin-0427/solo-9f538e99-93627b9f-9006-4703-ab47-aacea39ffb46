# -*- coding: utf-8 -*-
"""相容性判定与版号可行划分求解测试（含无解、更正重判）。"""
import json
import os
import unittest

from app.engine import (
    BatchError,
    CONFIRMED_NEW,
    CONFIRMED_OLD,
    PENDING,
    UNSOLVABLE,
    build_batch,
    build_pair,
    normalize_response,
    recompute_compat,
    solve,
)

SAMPLE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sample")


def load(name):
    with open(os.path.join(SAMPLE_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


def make_pair():
    return build_pair(load("config_old.json"), load("config_new.json"))


class CompatTests(unittest.TestCase):
    def setUp(self):
        self.pair = make_pair()

    def test_shown_but_missing_answer_is_incompatible(self):
        raw = {
            "serial": "X1",
            "answers": {"Q1": "A"},
            "records": [
                {"qid": "Q1", "shown": True},
                {"qid": "Q2", "shown": True},
                {"qid": "Q3", "shown": False},
                {"qid": "Q4", "shown": True},
            ],
        }
        r = normalize_response(raw)
        recompute_compat(r, self.pair)
        self.assertFalse(r.compat_old)
        self.assertTrue(any("Q2" in v and "缺少已答项" in v for v in r.old_violations))

    def test_skipped_with_answer_is_incompatible(self):
        raw = {
            "serial": "X2",
            "answers": {"Q1": "B", "Q2": True, "Q4": True},
            "records": [
                {"qid": "Q1", "shown": True},
                {"qid": "Q2", "shown": False},
                {"qid": "Q3", "shown": False},
                {"qid": "Q4", "shown": True},
            ],
        }
        r = normalize_response(raw)
        recompute_compat(r, self.pair)
        self.assertFalse(r.compat_old)
        self.assertTrue(any("Q2" in v and "跳过" in v for v in r.old_violations))

    def test_condition_mismatch_record(self):
        # 旧版 Q2 仅在 Q1=A 时展示；Q1=B 却记录 Q2 展示
        raw = {
            "serial": "X3",
            "answers": {"Q1": "B", "Q2": False, "Q4": True},
            "records": [
                {"qid": "Q1", "shown": True},
                {"qid": "Q2", "shown": True},
                {"qid": "Q3", "shown": False},
                {"qid": "Q4", "shown": True},
            ],
        }
        r = normalize_response(raw)
        recompute_compat(r, self.pair)
        self.assertFalse(r.compat_old)
        self.assertTrue(any("Q2" in v for v in r.old_violations))

    def test_value_out_of_range_and_bad_option(self):
        raw = {
            "serial": "X4",
            "answers": {"Q1": "Z", "Q4": True},
            "records": [
                {"qid": "Q1", "shown": True},
                {"qid": "Q2", "shown": False},
                {"qid": "Q3", "shown": False},
                {"qid": "Q4", "shown": True},
            ],
        }
        r = normalize_response(raw)
        recompute_compat(r, self.pair)
        self.assertFalse(r.compat_old)
        self.assertTrue(any("选项" in v for v in r.old_violations))

    def test_missing_record_defaults_to_skipped(self):
        # 未列出的题目按“跳过”处理：旧版 4 题全列，本身合法
        raw = {
            "serial": "X5",
            "answers": {"Q1": "B", "Q4": True},
            "records": [
                {"qid": "Q1", "shown": True},
                {"qid": "Q2", "shown": False},
                {"qid": "Q3", "shown": False},
                {"qid": "Q4", "shown": True},
            ],
        }
        r = normalize_response(raw)
        recompute_compat(r, self.pair)
        self.assertTrue(r.compat_old)

    def test_extra_question_record_is_incompatible_with_smaller_version(self):
        # 新版独有题 Q5 出现在答卷里 => 与旧版不相容（版本外题号，严进）
        raw = {
            "serial": "X5b",
            "answers": {"Q1": "A", "Q2": True, "Q3": 40, "Q4": True, "Q5": False},
            "records": [
                {"qid": "Q1", "shown": True},
                {"qid": "Q2", "shown": True},
                {"qid": "Q3", "shown": True},
                {"qid": "Q4", "shown": True},
                {"qid": "Q5", "shown": True},
            ],
        }
        r = normalize_response(raw)
        recompute_compat(r, self.pair)
        self.assertFalse(r.compat_old)
        self.assertTrue(any("Q5" in v for v in r.old_violations))
        self.assertTrue(r.compat_new)

    def test_shown_qids_compat_form(self):
        # shown_qids 形式：Q1、Q4 展示，Q2、Q3 跳过
        raw = {"serial": "X6", "answers": {"Q1": "B", "Q4": True}, "shown_qids": ["Q1", "Q4"]}
        r = normalize_response(raw)
        recompute_compat(r, self.pair)
        self.assertTrue(r.compat_old)
        self.assertFalse(r.compat_new)

    def test_skipped_predecessor_forces_skip(self):
        # 新版 Q3 无条件展示；若记录 Q3 跳过则新版不相容（旧版可因 Q2 跳过而跳过 Q3）
        raw = {
            "serial": "X7",
            "answers": {"Q1": "A", "Q2": False, "Q4": True},
            "records": [
                {"qid": "Q1", "shown": True},
                {"qid": "Q2", "shown": True},
                {"qid": "Q3", "shown": False},
                {"qid": "Q4", "shown": True},
            ],
        }
        r = normalize_response(raw)
        recompute_compat(r, self.pair)
        self.assertTrue(r.compat_old)
        self.assertFalse(r.compat_new)


class SolverTests(unittest.TestCase):
    def setUp(self):
        self.pair = make_pair()

    def _verdicts(self, batch):
        return {r.serial: r.verdict for r in batch.responses}

    def test_solvable_batch_classification(self):
        batch = build_batch(load("batch_solvable.json"), self.pair)
        self.assertTrue(batch.solvable)
        v = self._verdicts(batch)
        self.assertEqual(v["R0001"], CONFIRMED_OLD)
        self.assertEqual(v["R0002"], CONFIRMED_OLD)
        self.assertEqual(v["R0003"], CONFIRMED_OLD)
        self.assertEqual(v["R0004"], CONFIRMED_OLD)
        self.assertEqual(v["R0005"], CONFIRMED_NEW)
        self.assertEqual(v["R0006"], CONFIRMED_NEW)
        self.assertEqual(v["R0007"], CONFIRMED_NEW)
        self.assertEqual(v["R0008"], CONFIRMED_OLD)
        self.assertEqual(batch.count_confirmed_new, 3)
        self.assertEqual(batch.count_pending, 0)
        self.assertEqual(batch.count_confirmed_old, 5)

    def _both_compatible_response(self, serial):
        # 构造与两版都相容的答卷：两版题目集合相同（用自定义配置对）
        return {
            "serial": serial,
            "answers": {"Q1": True, "Q2": False},
            "records": [
                {"qid": "Q1", "shown": True},
                {"qid": "Q2", "shown": True},
            ],
        }

    def test_pending_when_ambiguous_segment_has_slack(self):
        # 同构两版（题号集相同、跳题条件不同），构造 both-compatible 点
        old_cfg = {
            "version": "old-x",
            "questions": [
                {"qid": "Q1", "qtype": "bool"},
                {"qid": "Q2", "qtype": "bool", "condition": "Q1 == true"},
            ],
        }
        new_cfg = {
            "version": "new-x",
            "questions": [
                {"qid": "Q1", "qtype": "bool"},
                {"qid": "Q2", "qtype": "bool"},
            ],
        }
        pair = build_pair(old_cfg, new_cfg)
        # Q1=true,Q2=false 展示两题：旧版要求 Q1=true 才展示 Q2（满足）；新版无条件（满足）
        both = self._both_compatible_response("B1")
        # 仅新版：旧版 Q1=false 时 Q2 必须跳过，本份却展示 Q2
        new_only = {
            "serial": "N1",
            "answers": {"Q1": False, "Q2": True},
            "records": [
                {"qid": "Q1", "shown": True},
                {"qid": "Q2", "shown": True},
            ],
        }
        batch = build_batch({"responses": [both, new_only]}, pair)
        self.assertTrue(batch.solvable)
        v = self._verdicts(batch)
        # 可行新版段：[B1,N1] 或 [N1,N1] => B1 待核，N1 确定新版
        self.assertEqual(v["B1"], PENDING)
        self.assertEqual(v["N1"], CONFIRMED_NEW)

    def test_no_new_version_at_all(self):
        # 全部仅旧版相容 => 空段是唯一可行划分 => 全部确定旧版
        payload = load("batch_solvable.json")
        payload["responses"] = [
            dict(x) for x in payload["responses"]
            if x["serial"] in ("R0001", "R0002", "R0003", "R0008")
        ]
        batch = build_batch(payload, self.pair)
        self.assertTrue(batch.solvable)
        self.assertTrue(all(r.verdict == CONFIRMED_OLD for r in batch.responses))
        self.assertEqual(batch.feasible_runs, [])

    def test_all_ambiguous_is_pending_and_empty_segment_allowed(self):
        # 没有任何 must-new 点时，空段（全旧）也是可行划分 => 无人确定新版
        old_cfg = {
            "version": "old-y",
            "questions": [
                {"qid": "Q1", "qtype": "bool"},
                {"qid": "Q2", "qtype": "bool", "condition": "Q1 == true"},
            ],
        }
        new_cfg = {
            "version": "new-y",
            "questions": [
                {"qid": "Q1", "qtype": "bool"},
                {"qid": "Q2", "qtype": "bool"},
            ],
        }
        pair = build_pair(old_cfg, new_cfg)
        one = self._both_compatible_response("B1")
        payload = {"responses": [one, dict(one, serial="B2")]}
        batch = build_batch(payload, pair)
        self.assertTrue(batch.solvable)
        self.assertTrue(all(r.verdict == PENDING for r in batch.responses))

    def test_unsolvable_reports_blocking_serials(self):
        batch = build_batch(load("batch_unsolvable.json"), self.pair)
        self.assertFalse(batch.solvable)
        self.assertEqual(batch.stuck_serials, ["R1002"])
        self.assertTrue(all(r.verdict == UNSOLVABLE for r in batch.responses))

    def test_response_incompatible_with_both_blocks(self):
        bad = {
            "serial": "R9001",
            "answers": {"Q1": "Z"},
            "records": [
                {"qid": "Q1", "shown": True},
                {"qid": "Q2", "shown": False},
                {"qid": "Q3", "shown": False},
                {"qid": "Q4", "shown": False},
            ],
        }
        good = load("batch_solvable.json")["responses"][0]
        batch = build_batch({"responses": [good, bad]}, self.pair)
        self.assertFalse(batch.solvable)
        self.assertEqual(batch.stuck_serials, ["R9001"])

    def test_duplicate_serial_rejected(self):
        payload = load("batch_solvable.json")
        payload["responses"].append(dict(payload["responses"][0]))
        with self.assertRaises(BatchError):
            build_batch(payload, self.pair)

    def test_batch_size_limit(self):
        one = load("batch_solvable.json")["responses"][0]
        payload = {"responses": [dict(one, serial=f"R{i:05d}") for i in range(2001)]}
        with self.assertRaises(BatchError) as ctx:
            build_batch(payload, self.pair)
        self.assertIn("2000", str(ctx.exception))

    def test_solver_is_pure_over_compat_flags(self):
        # 直接构造 compat 标志验证分段逻辑：N-only 分散在两段 => 无解
        from app.engine.solver import ResponseData, solve as _solve
        def rd(serial, old, new):
            d = ResponseData(serial=serial, answers={}, records={}, raw={"serial": serial})
            d.compat_old, d.compat_new = old, new
            return d
        data = [rd("a", False, True), rd("b", True, False), rd("c", False, True)]
        result = _solve(data)
        self.assertFalse(result.solvable)
        self.assertIn("b", result.stuck_serials)


class CorrectionRejudgeTests(unittest.TestCase):
    def setUp(self):
        self.pair = make_pair()
        # 同构配置对（题号集相同），便于构造两版都相容的答卷
        self.same_pair = build_pair(
            {
                "version": "old-x",
                "questions": [
                    {"qid": "Q1", "qtype": "bool"},
                    {"qid": "Q2", "qtype": "bool", "condition": "Q1 == true"},
                ],
            },
            {
                "version": "new-x",
                "questions": [
                    {"qid": "Q1", "qtype": "bool"},
                    {"qid": "Q2", "qtype": "bool"},
                ],
            },
        )

    def test_correction_only_rejudges_one_and_updates_verdicts(self):
        # B = 两版相容（Q1=true,Q2=false，两题都展示）
        both = {
            "serial": "B1",
            "answers": {"Q1": True, "Q2": False},
            "records": [
                {"qid": "Q1", "shown": True},
                {"qid": "Q2", "shown": True},
            ],
        }
        # N = 仅新版（Q1=false 却展示 Q2，旧版条件不允许）
        new_only = {
            "serial": "N1",
            "answers": {"Q1": False, "Q2": True},
            "records": [
                {"qid": "Q1", "shown": True},
                {"qid": "Q2", "shown": True},
            ],
        }
        batch = build_batch({"responses": [both, new_only]}, self.same_pair)
        responses = list(batch.responses)
        others_before = {r.serial: (r.compat_old, r.compat_new, r.verdict)
                         for r in responses if r.serial != "B1"}
        self.assertEqual(next(r for r in responses if r.serial == "B1").verdict, PENDING)

        # 把 B1 更正为另一份两版相容的答卷（改答案内容，结论仍待核，revision 由 store 递增）
        corrected_raw = {
            "serial": "B1",
            "answers": {"Q1": True, "Q2": True},
            "records": [
                {"qid": "Q1", "shown": True},
                {"qid": "Q2", "shown": True},
            ],
        }
        corrected = normalize_response(corrected_raw)
        recompute_compat(corrected, self.same_pair)
        self.assertTrue(corrected.compat_old and corrected.compat_new)

        idx = next(i for i, r in enumerate(responses) if r.serial == "B1")
        responses[idx] = corrected
        new_batch = solve(responses)
        self.assertTrue(new_batch.solvable)
        # 其余答卷的相容标志与结论完全未被改写
        for r in new_batch.responses:
            if r.serial != "B1":
                self.assertEqual(
                    (r.compat_old, r.compat_new, r.verdict), others_before[r.serial]
                )
        v = {r.serial: r.verdict for r in new_batch.responses}
        self.assertEqual(v["B1"], PENDING)
        self.assertEqual(v["N1"], CONFIRMED_NEW)

    def test_correction_on_real_sample_bumps_without_touching_others(self):
        from app.engine.store import BatchStore
        store = BatchStore()
        snap = store.load_batch(load("batch_solvable.json"), self.pair)
        before = {r.serial: r.verdict for r in snap.responses if r.serial != "R0007"}
        new_snap = store.correct("R0007", load("correction_R0007_ok.json"))
        self.assertEqual(new_snap.revision, snap.revision + 1)
        after = {r.serial: r.verdict for r in new_snap.responses if r.serial != "R0007"}
        self.assertEqual(before, after)
        target = next(r for r in new_snap.responses if r.serial == "R0007")
        self.assertEqual(target.verdict, CONFIRMED_NEW)

    def test_correction_causing_unsolvable_is_rejected_by_store(self):
        from app.engine.store import BatchStore, StoreError
        store = BatchStore()
        snap = store.load_batch(load("batch_solvable.json"), self.pair)
        rev_before = snap.revision

        # 用样例中的“坏更正”：R0006 变成仅旧版，把必须新版点 R0005 与 R0007 隔开 => 无解
        verdicts_before = {r.serial: r.verdict for r in store.get().responses}
        with self.assertRaises(StoreError) as ctx:
            store.correct("R0006", load("correction_unsolvable.json"))
        self.assertIn("R0006", str(ctx.exception))
        # 原快照不变：revision、所有 verdict 均保持提交前状态
        self.assertEqual(store.get().revision, rev_before)
        verdicts_after = {r.serial: r.verdict for r in store.get().responses}
        self.assertEqual(verdicts_before, verdicts_after)
        still = next(r for r in store.get().responses if r.serial == "R0006")
        self.assertEqual(still.verdict, CONFIRMED_NEW)

    def test_correction_unknown_serial(self):
        from app.engine.store import BatchStore, StoreError
        store = BatchStore()
        store.load_batch(load("batch_solvable.json"), self.pair)
        with self.assertRaises(StoreError):
            store.correct("NOPE", {"serial": "NOPE", "answers": {}, "records": []})


if __name__ == "__main__":
    unittest.main()
