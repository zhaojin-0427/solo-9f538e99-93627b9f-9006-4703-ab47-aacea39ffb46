# -*- coding: utf-8 -*-
"""版号核查引擎与存储测试（纯标准库，无 Flask 也可运行）。"""
import json
import os
import random
import shutil
import sqlite3
import tempfile
import time
import unittest

from app.batchstore import (
    BatchNotFound,
    correct_response,
    create_batch,
    drop_batch,
    init_db,
    load_view,
    warehouse_snapshot,
)
from app.engine.conditions import evaluate_mask, parse_condition
from app.engine.versioncheck import (
    MAX_BATCH,
    STATUS_NEW,
    STATUS_OLD,
    STATUS_PENDING,
    BatchError,
    _ast_to_mask,
    build_version_config,
    classify,
    judge,
    normalize_serial,
    parse_response,
    parse_responses,
    sort_serials,
)

OLD_CFG = {
    "items": ["S1", "S2", "S3"],
    "followups": [
        {"qid": "Q1", "condition": "S1 AND NOT S2"},
        {"qid": "Q2", "condition": "S3 OR S1"},
    ],
}
NEW_CFG = {
    "items": ["S1", "S2", "S3"],
    "followups": [
        {"qid": "Q1", "condition": "S1 AND NOT S2"},
        {"qid": "Q2", "condition": "S3 AND NOT S1"},
    ],
}


def make_batch_payload():
    """12 份：101-103 必旧，104-110 必新（105/107/108 在跨度内），111-112 待核。"""
    with open(os.path.join(os.path.dirname(__file__), "..",
                           "sample", "version_batch.json"), encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# 流水号
# ---------------------------------------------------------------------------

class SerialTests(unittest.TestCase):
    def test_normalize_accepts_str_and_int(self):
        self.assertEqual(normalize_serial(" A001 "), "A001")
        self.assertEqual(normalize_serial(7), "7")
        self.assertEqual(normalize_serial("007"), "007")

    def test_normalize_rejects_bad_types(self):
        for bad in (True, False, None, 1.5, ["1"], {"s": 1}):
            with self.assertRaises(BatchError):
                normalize_serial(bad)
        with self.assertRaises(BatchError):
            normalize_serial("   ")

    def test_numeric_sort_when_all_integer_like(self):
        self.assertEqual(sort_serials(["10", "9", "100", "007"]),
                         ["007", "9", "10", "100"])

    def test_lexicographic_sort_when_mixed(self):
        self.assertEqual(sort_serials(["A10", "A9", "B1"]), ["A10", "A9", "B1"])
        # 只要有一个非纯数字，整批按字典序
        self.assertEqual(sort_serials(["10", "9", "X"]), ["10", "9", "X"])


# ---------------------------------------------------------------------------
# 答卷解析
# ---------------------------------------------------------------------------

class ParseResponseTests(unittest.TestCase):
    def test_followups_list_form(self):
        rec = parse_response({
            "serial": "1",
            "answers": {"S1": True},
            "followups": [{"qid": "Q1", "shown": True}, {"qid": "Q2", "shown": False}],
        })
        self.assertEqual(rec.shown, {"Q1": True, "Q2": False})
        self.assertEqual(rec.answers, {"S1": True})

    def test_shown_map_form(self):
        rec = parse_response({"serial": "1", "shown": {"Q1": False}})
        self.assertEqual(rec.shown, {"Q1": False})
        self.assertEqual(rec.answers, {})

    def test_missing_serial_or_records_rejected(self):
        with self.assertRaises(BatchError):
            parse_response({"shown": {"Q1": True}})
        with self.assertRaises(BatchError):
            parse_response({"serial": "1"})
        with self.assertRaises(BatchError):
            parse_response({"serial": "1",
                            "followups": [{"qid": "Q1", "shown": True}],
                            "shown": {"Q1": True}})

    def test_explicit_shown_required_and_no_dup_qid(self):
        with self.assertRaises(BatchError):
            parse_response({"serial": "1", "followups": [{"qid": "Q1"}]})
        with self.assertRaises(BatchError):
            parse_response({"serial": "1", "followups": [
                {"qid": "Q1", "shown": True}, {"qid": "Q1", "shown": False}]})

    def test_bad_answer_value_rejected(self):
        with self.assertRaises(BatchError):
            parse_response({"serial": "1", "answers": {"S1": "maybe"},
                            "shown": {"Q1": True}})


class ParseResponsesTests(unittest.TestCase):
    def test_limit_and_unique(self):
        one = {"serial": "1", "shown": {"Q1": True}}
        ok = [dict(one, serial=str(i)) for i in range(MAX_BATCH)]
        self.assertEqual(len(parse_responses(ok)), MAX_BATCH)
        too_many = [dict(one, serial=str(i)) for i in range(MAX_BATCH + 1)]
        with self.assertRaises(BatchError):
            parse_responses(too_many)
        with self.assertRaises(BatchError):
            parse_responses([one, dict(one)])  # 重复流水号
        with self.assertRaises(BatchError):
            parse_responses([])

    def test_sorted_by_serial(self):
        recs = parse_responses({
            "responses": [
                {"serial": "10", "shown": {"Q1": True}},
                {"serial": "9", "shown": {"Q1": True}},
                {"serial": "100", "shown": {"Q1": True}},
            ]
        })
        self.assertEqual([r.serial for r in recs], ["9", "10", "100"])


# ---------------------------------------------------------------------------
# 相容判定
# ---------------------------------------------------------------------------

class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.old = build_version_config(OLD_CFG, "old")
        self.new = build_version_config(NEW_CFG, "new")

    def test_hand_cases(self):
        old_only = parse_response({"serial": "1", "answers": {"S1": True},
                                   "shown": {"Q1": True, "Q2": True}})
        self.assertTrue(judge(self.old, old_only))
        self.assertFalse(judge(self.new, old_only))

        new_only = parse_response({"serial": "2", "answers": {},
                                   "shown": {"Q1": True, "Q2": False}})
        self.assertFalse(judge(self.old, new_only))
        self.assertTrue(judge(self.new, new_only))

        both = parse_response({"serial": "3", "answers": {},
                               "shown": {"Q1": False, "Q2": False}})
        self.assertTrue(judge(self.old, both))
        self.assertTrue(judge(self.new, both))

        neither = parse_response({"serial": "4", "answers": {"S1": False, "S3": False},
                                  "shown": {"Q1": False, "Q2": True}})
        self.assertFalse(judge(self.old, neither))
        self.assertFalse(judge(self.new, neither))

    def test_qid_set_must_match_version(self):
        # 新版多出的追问题没有记录 => 与新版不相容
        new_extra = build_version_config(
            {"items": ["S1"], "followups": [{"qid": "Q1", "condition": "S1"},
                                            {"qid": "Q3", "condition": "NOT S1"}]},
            "new",
        )
        rec = parse_response({"serial": "1", "shown": {"Q1": True}})
        self.assertFalse(judge(new_extra, rec))
        # 记录里多出版本没有的题号 => 不相容
        rec2 = parse_response({"serial": "2",
                               "shown": {"Q1": True, "Q9": False}})
        self.assertFalse(judge(new_extra, rec2))

    def test_unknown_answer_item_rejected(self):
        rec = parse_response({"serial": "1", "answers": {"ZZ": True},
                              "shown": {"Q1": True, "Q2": True}})
        self.assertFalse(judge(self.old, rec))

    def test_ast_to_mask_matches_evaluate_mask(self):
        """位掩码编译结果与逐掩码求值完全一致（随机条件交叉验证）。"""
        rng = random.Random(20261123)
        items = ["A", "B", "C", "D"]
        cfg = build_version_config({"items": items, "followups": []}, "old")
        index = {name: i for i, name in enumerate(items)}
        for _ in range(300):
            cond = _random_condition(rng, items)
            ast = parse_condition(cond, items)
            mask = _ast_to_mask(ast, cfg.item_masks, cfg.full_mask)
            for m in range(1 << len(items)):
                expect = evaluate_mask(ast, m, index)
                self.assertEqual(bool(mask & (1 << m)), expect, cond)

    def test_judge_matches_bruteforce(self):
        """judge 与“枚举全部补全”参考实现一致（随机配置 + 随机答卷）。"""
        rng = random.Random(7)
        for _ in range(120):
            items = [f"V{i}" for i in range(rng.randint(1, 5))]
            qids = [f"Q{i}" for i in range(rng.randint(1, 4))]
            cfg_raw = {
                "items": items,
                "followups": [{"qid": q, "condition": _random_condition(rng, items)}
                              for q in qids],
            }
            cfg = build_version_config(cfg_raw, "old")
            answers = {v: rng.random() < 0.5 for v in items if rng.random() < 0.6}
            shown = {q: rng.random() < 0.5 for q in qids}
            rec = parse_response({"serial": "x", "answers": answers, "shown": shown})

            index = {name: i for i, name in enumerate(items)}
            asts = {q: parse_condition(c["condition"], items)
                    for q, c in zip(qids, cfg_raw["followups"])}
            expect = False
            for m in range(1 << len(items)):
                if any(bool(m & (1 << index[k])) != v for k, v in answers.items()):
                    continue
                if all(evaluate_mask(asts[q], m, index) == shown[q] for q in qids):
                    expect = True
                    break
            self.assertEqual(judge(cfg, rec), expect, (cfg_raw, answers, shown))


def _random_condition(rng, items):
    def atom():
        if rng.random() < 0.15:
            return rng.choice(["true", "false"])
        v = rng.choice(items)
        return f"NOT {v}" if rng.random() < 0.3 else v

    def term():
        parts = [atom() for _ in range(rng.randint(1, 3))]
        return "(" + " AND ".join(parts) + ")" if len(parts) > 1 else parts[0]

    parts = [term() for _ in range(rng.randint(1, 3))]
    return "(" + " OR ".join(parts) + ")" if len(parts) > 1 else parts[0]


# ---------------------------------------------------------------------------
# 连续段划分
# ---------------------------------------------------------------------------

def entries(bits):
    """bits: 串，如 'O N B O'，O=仅旧 N=仅新 B=两可 X=两否。"""
    table = {
        "O": (True, False),
        "N": (False, True),
        "B": (True, True),
        "X": (False, False),
    }
    out = []
    for i, token in enumerate(bits.split(), 1):
        o, n = table[token]
        out.append((str(i), o, n))
    return out


class ClassifyTests(unittest.TestCase):
    def test_all_flexible_all_pending(self):
        r = classify(entries("B B B"))
        self.assertTrue(r.feasible)
        self.assertEqual(set(r.statuses.values()), {STATUS_PENDING})
        self.assertEqual(r.warehouse, [])
        self.assertIsNone(r.must_new_span)

    def test_must_new_span_forces_inside(self):
        r = classify(entries("O N B B N O"))
        self.assertTrue(r.feasible)
        self.assertEqual(
            [r.statuses[str(i)] for i in range(1, 7)],
            [STATUS_OLD, STATUS_NEW, STATUS_NEW, STATUS_NEW, STATUS_NEW, STATUS_OLD],
        )
        self.assertEqual(r.warehouse, ["2", "3", "4", "5"])
        self.assertEqual(r.must_new_span, ("2", "5"))
        self.assertEqual(r.new_segment_bounds, ("2", "5"))

    def test_flexible_beyond_barrier_is_old(self):
        # 位置 4 必旧，位置 5 虽两可但无法与必新段相连
        r = classify(entries("N B O B B"))
        self.assertTrue(r.feasible)
        self.assertEqual(r.statuses["4"], STATUS_OLD)
        self.assertEqual(r.statuses["5"], STATUS_OLD)
        self.assertEqual(r.statuses["2"], STATUS_PENDING)
        self.assertEqual(r.new_segment_bounds, ("1", "2"))

    def test_pending_on_both_sides(self):
        r = classify(entries("B N B"))
        self.assertEqual(r.statuses["1"], STATUS_PENDING)
        self.assertEqual(r.statuses["2"], STATUS_NEW)
        self.assertEqual(r.statuses["3"], STATUS_PENDING)
        self.assertEqual(r.warehouse, ["2"])

    def test_neither_blocks(self):
        r = classify(entries("O X N"))
        self.assertFalse(r.feasible)
        self.assertEqual(len(r.blockers), 1)
        self.assertIn("2", r.blockers[0])
        self.assertIn("均不相容", r.blockers[0])

    def test_must_old_inside_span_blocks(self):
        r = classify(entries("N B O B N"))
        self.assertFalse(r.feasible)
        self.assertEqual(len(r.blockers), 1)
        msg = r.blockers[0]
        self.assertIn("3", msg)   # 夹断者
        self.assertIn("1", msg)   # 跨度端点
        self.assertIn("5", msg)

    def test_neither_and_span_blockers_together(self):
        r = classify(entries("X N O N"))
        self.assertFalse(r.feasible)
        self.assertEqual(len(r.blockers), 2)

    def test_counts(self):
        r = classify(entries("O N B"))
        self.assertEqual(r.counts,
                         {"total": 3, "old": 1, "new": 1, "pending": 1})


# ---------------------------------------------------------------------------
# 存储：导入 / 更正 / 下载
# ---------------------------------------------------------------------------

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vc-test-")
        self.db = os.path.join(self.dir, "test.sqlite3")
        init_db(self.db)
        payload = make_batch_payload()
        self.old_raw = payload["old_config"]
        self.new_raw = payload["new_config"]
        self.responses = payload["responses"]

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _create(self):
        return create_batch(self.db, self.old_raw, self.new_raw, self.responses)[0]

    def _dump_rows(self, batch_id):
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute(
                "SELECT serial, ord, payload, old_ok, new_ok FROM responses"
                " WHERE batch_id = ? ORDER BY ord", (batch_id,)).fetchall()
            version = conn.execute(
                "SELECT state_version FROM batches WHERE id = ?",
                (batch_id,)).fetchone()[0]
        finally:
            conn.close()
        return version, rows

    def test_create_and_classify(self):
        batch_id = self._create()
        view = load_view(self.db, batch_id)
        self.assertEqual(view.state_version, 1)
        c = view.classification.counts
        self.assertEqual((c["new"], c["old"], c["pending"]), (7, 3, 2))
        self.assertEqual(view.classification.must_new_span, ("104", "110"))
        self.assertEqual(view.classification.new_segment_bounds, ("104", "112"))
        self.assertEqual(view.detail("111").status, STATUS_PENDING)
        self.assertEqual(view.detail("105").status, STATUS_NEW)

    def test_create_infeasible_not_stored(self):
        bad = [dict(r) for r in self.responses]
        bad[0] = {"serial": "101", "answers": {"S1": False, "S3": False},
                  "shown": {"Q1": False, "Q2": True}}  # 两版均不相容
        with self.assertRaises(BatchError) as ctx:
            create_batch(self.db, self.old_raw, self.new_raw, bad)
        self.assertIn("101", ctx.exception.blockers[0])
        conn = sqlite3.connect(self.db)
        try:
            n = conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 0)

    def test_correct_updates_only_one_row(self):
        batch_id = self._create()
        _, rows_before = self._dump_rows(batch_id)
        rec, result, version = correct_response(self.db, batch_id, {
            "serial": "111", "answers": {"S1": True},
            "shown": {"Q1": True, "Q2": True},
        })
        self.assertEqual(version, 2)
        self.assertEqual(rec.serial, "111")
        # 111 变必旧；112 被 111 隔断，随之变为确定旧版
        self.assertEqual(result.statuses["111"], STATUS_OLD)
        self.assertEqual(result.statuses["112"], STATUS_OLD)
        self.assertEqual(result.counts["pending"], 0)

        _, rows_after = self._dump_rows(batch_id)
        before = {r[0]: r for r in rows_before}
        after = {r[0]: r for r in rows_after}
        changed = [s for s in before if before[s] != after[s]]
        self.assertEqual(changed, ["111"], "只有被更正的一行允许变化")

    def test_correct_rejudges_only_the_one_response(self):
        """更正时相容判定只针对被更正那一份（2 次 judge：旧版、新版各一）。"""
        import app.batchstore as store

        batch_id = self._create()
        calls = []
        real_judge = store.judge

        def counting_judge(cfg, rec):
            calls.append(rec.serial)
            return real_judge(cfg, rec)

        store.judge = counting_judge
        try:
            correct_response(self.db, batch_id, {
                "serial": "111", "answers": {"S1": True},
                "shown": {"Q1": True, "Q2": True},
            })
        finally:
            store.judge = real_judge
        self.assertEqual(sorted(calls), ["111", "111"])

    def test_correct_noop_keeps_state_version(self):
        batch_id = self._create()
        original = self.responses[4]  # serial 105
        _, _, version = correct_response(self.db, batch_id, original)
        self.assertEqual(version, 1)

    def test_correct_infeasible_rolls_back(self):
        batch_id = self._create()
        version_before, rows_before = self._dump_rows(batch_id)
        with self.assertRaises(BatchError) as ctx:
            correct_response(self.db, batch_id, {
                "serial": "105", "answers": {},
                "shown": {"Q1": True, "Q2": True},  # 变为必旧，夹断 104~110
            })
        msg = "；".join(ctx.exception.blockers)
        self.assertIn("105", msg)
        self.assertIn("104", msg)
        self.assertIn("110", msg)
        version_after, rows_after = self._dump_rows(batch_id)
        self.assertEqual(version_before, version_after)
        self.assertEqual(rows_before, rows_after)

    def test_correct_neither_blocks(self):
        batch_id = self._create()
        with self.assertRaises(BatchError) as ctx:
            correct_response(self.db, batch_id, {
                "serial": "105", "answers": {"S1": False, "S3": False},
                "shown": {"Q1": False, "Q2": True},
            })
        self.assertIn("均不相容", ctx.exception.blockers[0])

    def test_correct_unknown_serial(self):
        batch_id = self._create()
        with self.assertRaises(BatchError):
            correct_response(self.db, batch_id, {
                "serial": "999", "shown": {"Q1": True, "Q2": True}})

    def test_correct_missing_batch(self):
        with self.assertRaises(BatchNotFound):
            correct_response(self.db, "no-such-batch", {
                "serial": "1", "shown": {"Q1": True}})

    def test_warehouse_only_definite_new_and_consistent(self):
        batch_id = self._create()
        state_version, generated_at, entries = warehouse_snapshot(self.db, batch_id)
        self.assertEqual(state_version, 1)
        self.assertTrue(generated_at)
        serials = [e["serial"] for e in entries]
        self.assertEqual(serials, ["104", "105", "106", "107", "108", "109", "110"])
        for e in entries:
            self.assertEqual(e["version"], "new")
            self.assertIn("answers", e)
            self.assertIn("shown", e)

    def test_warehouse_snapshot_isolated_from_concurrent_correction(self):
        """下载读事务进行期间发生更正：读到的是同一个已提交状态。"""
        batch_id = self._create()
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN")
            v1 = conn.execute("SELECT state_version FROM batches WHERE id = ?",
                              (batch_id,)).fetchone()[0]
            self.assertEqual(v1, 1)
            # 另一连接提交更正（状态进入 v2）
            correct_response(self.db, batch_id, {
                "serial": "111", "answers": {"S1": True},
                "shown": {"Q1": True, "Q2": True}})
            # 本读事务仍看到 v1 快照
            again = conn.execute("SELECT state_version FROM batches WHERE id = ?",
                                 (batch_id,)).fetchone()[0]
            self.assertEqual(again, 1)
            conn.rollback()
        finally:
            conn.close()
        # 事务结束后是新状态
        view = load_view(self.db, batch_id)
        self.assertEqual(view.state_version, 2)

    def test_drop_batch(self):
        batch_id = self._create()
        drop_batch(self.db, batch_id)
        with self.assertRaises(BatchNotFound):
            load_view(self.db, batch_id)

    def test_full_batch_performance(self):
        """2000 份满批导入与单份更正的耗时冒烟（应远小于交互可感知阈值）。"""
        responses = []
        for i in range(MAX_BATCH):
            if i < MAX_BATCH // 2:
                # 只与旧版相容（必旧），排在流水号前半段
                responses.append({
                    "serial": str(10000 + i),
                    "answers": {"S1": True, "S2": True},
                    "shown": {"Q1": False, "Q2": True},
                })
            else:
                # 只与新版相容（必新），排在后半段
                responses.append({
                    "serial": str(10000 + i),
                    "answers": {"S1": True, "S2": False},
                    "shown": {"Q1": True, "Q2": False},
                })

        start = time.perf_counter()
        batch_id, result = create_batch(self.db, self.old_raw, self.new_raw, responses)
        elapsed_import = time.perf_counter() - start
        self.assertTrue(result.feasible)
        self.assertEqual(result.counts["total"], MAX_BATCH)

        start = time.perf_counter()
        correct_response(self.db, batch_id, {
            "serial": "10001", "answers": {"S1": True},
            "shown": {"Q1": True, "Q2": True},
        })
        elapsed_correct = time.perf_counter() - start
        # 宽松阈值：导入 < 10s，更正 < 2s（实测为毫秒级）
        self.assertLess(elapsed_import, 10.0)
        self.assertLess(elapsed_correct, 2.0)
        print(f"\n[perf] 导入 {MAX_BATCH} 份 {elapsed_import:.3f}s，"
              f"单份更正 {elapsed_correct:.3f}s")


if __name__ == "__main__":
    unittest.main()
