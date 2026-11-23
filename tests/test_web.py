# -*- coding: utf-8 -*-
"""Flask 路由集成测试（无 Flask 环境时自动跳过）。"""
import json
import os
import unittest

try:
    import importlib

    from app.app import create_app
    store_module = importlib.import_module("app.engine.store")

    FLASK_AVAILABLE = True
except Exception:  # pragma: no cover
    FLASK_AVAILABLE = False

SAMPLE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sample")


def load(name):
    with open(os.path.join(SAMPLE_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


@unittest.skipUnless(FLASK_AVAILABLE, "Flask 未安装")
class WebFlowTests(unittest.TestCase):
    def setUp(self):
        store_module.batch_store.clear()
        app = create_app()
        app.config.update(TESTING=True)
        self.client = app.test_client()
        self.old_cfg = load("config_old.json")
        self.new_cfg = load("config_new.json")

    def _setup(self, batch=None):
        data = {
            "old_config_text": json.dumps(self.old_cfg, ensure_ascii=False),
            "new_config_text": json.dumps(self.new_cfg, ensure_ascii=False),
        }
        if batch is not None:
            data["batch_text"] = json.dumps(batch, ensure_ascii=False)
        return self.client.post("/setup", data=data)

    def test_full_flow_classification_and_query(self):
        resp = self._setup(load("batch_solvable.json"))
        self.assertEqual(resp.status_code, 302)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("确定新版", page)
        self.assertIn("确定旧版", page)
        self.assertIn("待核", page)
        self.assertIn("revision=1", page)

        page = self.client.get("/?serial=R0005").get_data(as_text=True)
        self.assertIn("R0005", page)
        # R0005 是确定新版
        segment = page.split('流水号 R0005', 1)[1]
        self.assertIn("确定新版", segment)

    def test_download_only_contains_confirmed_new(self):
        self._setup(load("batch_solvable.json"))
        resp = self.client.get("/download")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["X-Source-Revision"], "1")
        payload = json.loads(resp.get_data(as_text=True))
        serials = [r["serial"] for r in payload["responses"]]
        self.assertEqual(serials, ["R0005", "R0006", "R0007"])
        self.assertTrue(all(r["verdict"] == "confirmed_new" for r in payload["responses"]))
        self.assertEqual(payload["accepted_count"], 3)
        self.assertEqual(payload["total_in_batch"], 8)

    def test_correction_flow_bumps_revision_and_keeps_others(self):
        self._setup(load("batch_solvable.json"))
        correction = load("correction_R0007_ok.json")
        resp = self.client.post("/correct", data={
            "serial": "R0007",
            "corrected_text": json.dumps(correction, ensure_ascii=False),
        })
        self.assertEqual(resp.status_code, 302)
        page = self.client.get("/?serial=R0007").get_data(as_text=True)
        self.assertIn("revision=2", page)
        self.assertIn("确定新版", page)
        # 其余答卷未变：R0005 仍确定新版
        page2 = self.client.get("/?serial=R0005").get_data(as_text=True)
        self.assertIn("确定新版", page2)
        # 下载跟随新 revision；更正后 R0007 仍是确定新版，仍入仓
        payload = json.loads(self.client.get("/download").get_data(as_text=True))
        self.assertEqual(payload["source_revision"], 2)
        self.assertEqual(
            [r["serial"] for r in payload["responses"]],
            ["R0005", "R0006", "R0007"],
        )

    def test_unsolvable_correction_rejected_and_state_kept(self):
        self._setup(load("batch_solvable.json"))
        bad = {
            "serial": "R0006",
            "answers": {"Q1": "B", "Q4": True},
            "records": [
                {"qid": "Q1", "shown": True},
                {"qid": "Q2", "shown": False},
                {"qid": "Q3", "shown": False},
                {"qid": "Q4", "shown": True},
            ],
        }
        resp = self.client.post("/correct", data={
            "serial": "R0006",
            "corrected_text": json.dumps(bad, ensure_ascii=False),
        })
        self.assertEqual(resp.status_code, 302)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("更正被拒绝", page)
        self.assertIn("R0006", page)
        # 旧状态照旧：revision 仍是 1，下载内容不变
        payload = json.loads(self.client.get("/download").get_data(as_text=True))
        self.assertEqual(payload["source_revision"], 1)
        self.assertEqual(payload["accepted_count"], 3)

    def test_unsolvable_import_shows_blocking_serial(self):
        resp = self._setup(load("batch_unsolvable.json"))
        self.assertEqual(resp.status_code, 302)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("整批无解", page)
        self.assertIn("R1002", page)

    def test_download_blocked_when_unsolvable(self):
        self._setup(load("batch_unsolvable.json"))
        resp = self.client.get("/download")
        # 无解时重定向回主页并提示，不产出入仓文件
        self.assertEqual(resp.status_code, 302)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("整批无解", page)
        self.assertIn("不能生成入仓文件", page)

    def test_invalid_config_flashes_error(self):
        resp = self.client.post("/setup", data={
            "old_config_text": "{not json",
            "new_config_text": json.dumps(self.new_cfg),
        })
        self.assertEqual(resp.status_code, 302)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("配置校验失败", page)

    def test_oversized_batch_rejected(self):
        one = load("batch_solvable.json")["responses"][0]
        big = {"responses": [dict(one, serial=f"R{i:05d}") for i in range(2001)]}
        resp = self._setup(big)
        self.assertEqual(resp.status_code, 302)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("2000", page)

    def test_upload_then_correct_via_routes(self):
        # 先只建配置，再单独上传批次
        self._setup(batch=None)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("尚未导入答卷", page)
        resp = self.client.post("/upload", data={
            "batch_text": json.dumps(load("batch_solvable.json"), ensure_ascii=False),
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(store_module.batch_store.has_snapshot())

    def test_reset_clears_everything(self):
        self._setup(load("batch_solvable.json"))
        self.client.post("/reset")
        self.assertIsNone(store_module.batch_store.get())
        self.assertIsNone(store_module.batch_store.get_pair())

    def test_snapshot_consistency_under_concurrent_correction(self):
        # 并发更正与下载：每次下载内部的流水号与版号必须来自同一 revision
        import concurrent.futures

        self._setup(load("batch_solvable.json"))

        def download():
            payload = json.loads(self.client.get("/download").get_data(as_text=True))
            rev = payload["source_revision"]
            # 用当前 store 状态核对该快照自洽（revision 内结论一致）
            return rev, [r["serial"] for r in payload["responses"]]

        correction = load("correction_R0007_ok.json")

        def do_correct():
            return self.client.post("/correct", data={
                "serial": "R0007",
                "corrected_text": json.dumps(correction, ensure_ascii=False),
            }).status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(download) for _ in range(6)]
            futures += [pool.submit(do_correct) for _ in range(2)]
            results = [f.result() for f in futures]
        downloads = [r for r in results if isinstance(r, tuple)]
        revs = {rev for rev, _ in downloads}
        self.assertTrue(revs.issubset({1, 2, 3}))
        # 同一 revision 的下载内容必须完全一致
        by_rev = {}
        for rev, serials in downloads:
            by_rev.setdefault(rev, set()).add(tuple(serials))
        for rev, variants in by_rev.items():
            self.assertEqual(len(variants), 1, f"revision {rev} 下载内容不一致")


if __name__ == "__main__":
    unittest.main()
