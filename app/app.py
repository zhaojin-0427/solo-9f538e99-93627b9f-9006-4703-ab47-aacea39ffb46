# -*- coding: utf-8 -*-
"""问卷版号核查页 Flask 应用工厂。

路由：
- GET  /            核查主页（配置表单 / 批次结论 / 逐份查询 / 更正 / 下载入口）
- POST /setup       提交旧版+新版两份配置，并可同批上传答卷 JSON
- POST /upload      在已有配置上上传/替换答卷批次
- POST /correct     按流水号更正一份答卷（只重判这一份；无解则拒绝）
- GET  /download    下载入仓 JSON（只收“确定新版”，基于当前已提交快照）
- POST /reset       清空全部状态

状态保存在进程内单例 store（engine/store.py），由锁保证原子提交；
下载与更正互斥，单份下载内的答卷与版号来自同一个 revision 快照。
"""
from __future__ import annotations

import json
import os

from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from .engine import (
    BatchError,
    ConfigError,
    StoreError,
    VERDICT_LABELS,
    batch_store,
    build_pair,
    dump_snapshot_meta,
)


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me-in-production")

    def read_text(upload, field: str) -> str:
        try:
            return upload.read().decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ConfigError(f"{field} 不是 UTF-8 编码的文本文件：{exc}") from exc

    def parse_json_field(text: str, upload, field: str):
        raw_text = text.strip() if text else ""
        if upload and upload.filename:
            raw_text = read_text(upload, field)
        if not raw_text:
            raise ConfigError(f"请粘贴 {field} JSON 或上传文件")
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"{field} JSON 解析失败：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）"
            ) from exc

    def load_pair_from_form():
        old_raw = parse_json_field(
            request.form.get("old_config_text", ""),
            request.files.get("old_config_file"),
            "旧版配置",
        )
        new_raw = parse_json_field(
            request.form.get("new_config_text", ""),
            request.files.get("new_config_file"),
            "新版配置",
        )
        return build_pair(old_raw, new_raw)

    @app.route("/")
    def index():
        snap = batch_store.get()
        detail = None
        detail_serial = request.args.get("serial", "").strip()
        if snap is not None and detail_serial:
            detail = next(
                (r for r in snap.responses if r.serial == detail_serial), None
            )
            if detail is None:
                flash(f"批次中没有流水号 {detail_serial}。", "error")
        return render_template(
            "index.html",
            snap=snap,
            has_pair=batch_store.has_pair(),
            meta=dump_snapshot_meta(snap) if snap else None,
            detail=detail,
            verdict_labels=VERDICT_LABELS,
        )

    @app.route("/setup", methods=["POST"])
    def setup():
        """建立两版配置，并可选地同批导入答卷。"""
        try:
            pair = load_pair_from_form()
        except ConfigError as exc:
            flash(f"配置校验失败：{exc}", "error")
            return redirect(url_for("index"))

        batch_upload = request.files.get("batch_file")
        batch_text = request.form.get("batch_text", "")
        has_batch = (batch_upload and batch_upload.filename) or batch_text.strip()
        if not has_batch:
            # 仅建立配置，等待 /upload 导入答卷
            batch_store.set_pair_only(pair)
            flash("两版配置已建立，请上传该终端的答卷 JSON。", "ok")
            return redirect(url_for("index"))

        try:
            payload = parse_json_field(batch_text, batch_upload, "答卷批次")
            snap = batch_store.load_batch(payload, pair)
        except (ConfigError, BatchError) as exc:
            flash(f"答卷导入失败：{exc}。原数据未改动。", "error")
            return redirect(url_for("index"))

        _flash_batch_result(snap)
        return redirect(url_for("index"))

    @app.route("/upload", methods=["POST"])
    def upload():
        """在已有配置上上传/替换答卷批次。"""
        pair = batch_store.get_pair()
        if pair is None:
            flash("请先提交两版问卷配置。", "error")
            return redirect(url_for("index"))
        try:
            payload = parse_json_field(
                request.form.get("batch_text", ""),
                request.files.get("batch_file"),
                "答卷批次",
            )
            new_snap = batch_store.load_batch(payload, pair)
        except (ConfigError, BatchError) as exc:
            flash(f"答卷导入失败：{exc}。原数据未改动。", "error")
            return redirect(url_for("index"))
        _flash_batch_result(new_snap)
        return redirect(url_for("index"))

    @app.route("/correct", methods=["POST"])
    def correct():
        """按流水号更正一份答卷；只重判这一份，无解则拒绝并保留原状态。"""
        serial = request.form.get("serial", "").strip()
        if not serial:
            flash("请填写要更正的流水号。", "error")
            return redirect(url_for("index"))
        try:
            corrected = parse_json_field(
                request.form.get("corrected_text", ""),
                request.files.get("corrected_file"),
                "更正答卷",
            )
            snap = batch_store.correct(serial, corrected)
        except (ConfigError, BatchError, StoreError) as exc:
            flash(f"更正被拒绝：{exc}", "error")
            return redirect(url_for("index", serial=serial))
        flash(
            f"流水号 {serial} 已按更正后数据重判，状态版本号更新为 revision={snap.revision}；"
            "其余答卷未被扫描或改写。",
            "ok",
        )
        return redirect(url_for("index", serial=serial))

    @app.route("/download")
    def download():
        """下载入仓 JSON：只收在所有可行划分下都属新版的答卷。"""
        snap = batch_store.get()
        if snap is None:
            flash("尚未导入任何批次，无法下载。", "error")
            return redirect(url_for("index"))
        if not snap.batch.solvable:
            flash(
                "当前批次整批无解，没有可靠版号，不能生成入仓文件；"
                "请先更正卡住划分的流水号：" + "、".join(snap.batch.stuck_serials),
                "error",
            )
            return redirect(url_for("index"))
        try:
            payload = batch_store.export_payload()
        except StoreError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        filename = f"warehouse_rev{payload['source_revision']}.json"
        return Response(
            body,
            mimetype="application/json; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Source-Revision": str(payload["source_revision"]),
            },
        )

    @app.route("/reset", methods=["POST"])
    def reset():
        batch_store.clear()
        flash("已清空配置与全部批次数据。", "ok")
        return redirect(url_for("index"))

    def _flash_batch_result(snap):
        b = snap.batch
        if not b.solvable:
            flash(
                "整批无解，已保留数据但无法给出版号；卡住划分的流水号："
                + "、".join(b.stuck_serials),
                "error",
            )
            return
        flash(
            f"批次已核定（revision={snap.revision}，共 {b.count} 份）：确定新版 "
            f"{b.count_confirmed_new} 份、确定旧版 {b.count_confirmed_old} 份、"
            f"待核 {b.count_pending} 份。",
            "ok",
        )

    return app
