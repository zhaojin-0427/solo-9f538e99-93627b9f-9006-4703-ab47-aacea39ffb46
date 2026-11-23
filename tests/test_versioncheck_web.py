# -*- coding: utf-8 -*-
"""版号核查页 Flask 路由集成测试（无 Flask 环境时自动跳过；容器内完整执行）。"""
import json
import os
import shutil
import tempfile
import unittest

try:
    from app.app import create_app

    FLASK_AVAILABLE = True
except Exception:  # pragma: no cover - 本地最小环境
    FLASK_AVAILABLE = False


def sample_payload():
    path = os.path.join(os.path.dirname(__file__), "..",
                        "sample", "version_batch.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


@unittest.skipUnless(FLASK_AVAILABLE, "Flask 未安装")
class VersionCheckWebTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vc-web-")
        os.environ["BATCH_DB_PATH"] = os.path.join(self.dir, "web.sqlite3")
        app = create_app()
        app.config.update(TESTING=True)
        self.client = app.test_client()
        self.payload = sample_payload()

    def tearDown(self):
        os.environ.pop("BATCH_DB_PATH", None)
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- 辅助 -------------------------------------------------------------
    def _setup_combined(self, payload=None):
        data = payload if payload is not None else self.payload
        return self.client.post(
            "/version-check/setup",
            data={"responses_text": json.dumps(data, ensure_ascii=False)},
        )

    def _page(self):
        return self.client.get("/version-check").get_data(as_text=True)

    def _download(self):
        resp = self.client.get("/version-check/download")
        self.assertEqual(resp.status_code, 200)
        return json.loads(resp.get_data(as_text=True))

    # -- 用例 -------------------------------------------------------------
    def test_setup_and_classify_page(self):
        resp = self._setup_combined()
        self.assertEqual(resp.status_code, 302)
        page = self._page()
        self.assertIn("批次已建立", page)
        self.assertIn("确定新版", page)
        self.assertIn("确定旧版", page)
        self.assertIn("待核", page)
        self.assertIn("104", page)
        self.assertIn("已提交状态 v1", page)

    def test_setup_split_fields(self):
        resp = self.client.post("/version-check/setup", data={
            "old_config_text": json.dumps(self.payload["old_config"]),
            "new_config_text": json.dumps(self.payload["new_config"]),
            "responses_text": json.dumps({"responses": self.payload["responses"]}),
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn("批次已建立", self._page())

    def test_setup_missing_fields(self):
        resp = self.client.post("/version-check/setup", data={})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("请提供", self._page())

    def test_serial_query_detail(self):
        self._setup_combined()
        page = self.client.get(
            "/version-check?serial=111").get_data(as_text=True)
        self.assertIn("待核", page)
        self.assertIn("111", page)
        page = self.client.get(
            "/version-check?serial=999").get_data(as_text=True)
        self.assertIn("不在当前批次内", page)

    def test_download_only_definite_new(self):
        self._setup_combined()
        doc = self._download()
        self.assertEqual(doc["state_version"], 1)
        self.assertEqual(doc["count"], 7)
        serials = [e["serial"] for e in doc["responses"]]
        self.assertEqual(
            serials, ["104", "105", "106", "107", "108", "109", "110"])
        for e in doc["responses"]:
            self.assertEqual(e["version"], "new")

    def test_correction_updates_state(self):
        self._setup_combined()
        resp = self.client.post("/version-check/correct", data={
            "response_text": json.dumps({
                "serial": "111", "answers": {"S1": True},
                "shown": {"Q1": True, "Q2": True}}),
        })
        self.assertEqual(resp.status_code, 302)
        page = self._page()
        self.assertIn("已提交状态 v2", page)
        self.assertIn("已更正", page)
        # 112 受 111 隔断影响，一并变为确定旧版
        detail = self.client.get(
            "/version-check?serial=112").get_data(as_text=True)
        self.assertIn("确定旧版", detail)

    def test_infeasible_correction_rejected_and_download_unchanged(self):
        self._setup_combined()
        before = self._download()
        resp = self.client.post("/version-check/correct", data={
            "response_text": json.dumps({
                "serial": "105", "answers": {},
                "shown": {"Q1": True, "Q2": True}}),
        })
        self.assertEqual(resp.status_code, 302)
        page = self._page()
        self.assertIn("更正被拒绝", page)
        self.assertIn("105", page)
        self.assertIn("104", page)
        self.assertIn("110", page)
        after = self._download()
        self.assertEqual(before, after)  # 原数据与下载结果照旧

    def test_infeasible_import_keeps_previous_batch(self):
        self._setup_combined()
        before = self._download()
        bad = {
            "old_config": self.payload["old_config"],
            "new_config": self.payload["new_config"],
            "responses": [
                {"serial": "1", "answers": {"S1": False, "S3": False},
                 "shown": {"Q1": False, "Q2": True}},
            ],
        }
        self._setup_combined(bad)
        page = self._page()
        self.assertIn("导入被拒绝", page)
        self.assertIn("均不相容", page)
        after = self._download()
        self.assertEqual(before, after)

    def test_reset(self):
        self._setup_combined()
        self.client.post("/version-check/reset")
        page = self._page()
        self.assertIn("批次已清空", page)
        self.assertNotIn("已提交状态", page)
        # 重置后下载回到“请先建立批次”
        resp = self.client.get("/version-check/download")
        self.assertEqual(resp.status_code, 302)

    def test_old_tool_reset_keeps_version_batch(self):
        """单份核查页的会话重置不应丢掉版号核查批次。"""
        self._setup_combined()
        self.client.post("/reset")  # 旧工具的重置
        page = self._page()
        self.assertIn("已提交状态 v1", page)

    def test_nav_between_pages(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("版号核查", page)
        page = self._page()
        self.assertIn("单份纳入核查", page)


if __name__ == "__main__":
    unittest.main()
