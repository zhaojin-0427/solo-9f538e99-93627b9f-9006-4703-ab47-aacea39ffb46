# -*- coding: utf-8 -*-
"""Flask 核查页应用工厂。

单份纳入核查（会话数据存于签名 cookie session）：
- config_json：静态配置（筛查项、追问题条件、纳入条件）
- answers：当前已确认的筛查项答案（含旧答卷已答项 + 后续电话补问项）
- last_submission：最近一次被拒绝的提交，用于“保留提交前结果 + 冲突提示”

版号核查：
- vc_batch_id：当前批次 id；批次数据（两版配置、答卷、相容位、状态号）存于
  服务端 SQLite 库（见 batchstore.py），多 worker 共享，下载走事务快照。
"""
from __future__ import annotations

import json
import os

from flask import Flask, Response, flash, redirect, render_template, request, session, url_for

from .batchstore import (
    BatchNotFound,
    correct_response,
    create_batch,
    drop_batch,
    init_db,
    load_view,
    warehouse_snapshot,
)
from .engine import (
    MAX_BATCH,
    STATUS_LABELS,
    AnalysisConfig,
    BatchError,
    ConfigError,
    analyze,
    build_config,
    config_to_jsonable,
    replace_followups,
    validate_archive,
)


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me-in-production")

    # 版号核查批次库（SQLite 单文件，多 worker 共享；可用 BATCH_DB_PATH 覆盖）
    db_path = os.environ.get("BATCH_DB_PATH") or os.path.join(
        app.instance_path, "versioncheck.sqlite3"
    )
    init_db(db_path)

    def clear_session_keep_batch():
        """清空单份核查会话，但保留版号核查批次指针（批次数据在服务端库中）。"""
        vc_id = session.get("vc_batch_id")
        session.clear()
        if vc_id:
            session["vc_batch_id"] = vc_id

    def get_state():
        cfg_json = session.get("config_json")
        cfg = build_config(cfg_json) if cfg_json else None
        answers = session.get("answers", {})
        return cfg, answers

    def require_state():
        """供 POST 路由使用：会话损坏时清空并返回 None。"""
        try:
            cfg, answers = get_state()
        except ConfigError as exc:
            clear_session_keep_batch()
            flash(f"会话中的配置已损坏，已重置：{exc}", "error")
            return None, {}
        if cfg is None:
            flash("请先建立核查配置。", "error")
        return cfg, answers

    def save_config(cfg: AnalysisConfig):
        session["config_json"] = config_to_jsonable(cfg)

    def read_upload(upload, field: str) -> str:
        try:
            return upload.read().decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ConfigError(f"{field} 不是 UTF-8 编码的文本文件：{exc}") from exc

    @app.route("/")
    def index():
        cfg = None
        if session.get("config_json"):
            try:
                cfg, answers = get_state()
            except ConfigError as exc:
                clear_session_keep_batch()
                flash(f"会话中的配置已损坏，已重置：{exc}", "error")
                cfg = None
        if cfg is None:
            answers, result = {}, None
        else:
            result = analyze(cfg, answers)
        return render_template(
            "index.html",
            cfg=cfg,
            answers=answers,
            result=result,
            last_submission=session.get("last_submission"),
        )

    @app.route("/setup", methods=["POST"])
    def setup():
        """接收配置文本（JSON）或上传文件，建立核查任务并可附带旧答卷。"""
        raw_text = request.form.get("config_text", "").strip()
        upload = request.files.get("config_file")
        if upload and upload.filename:
            try:
                raw_text = read_upload(upload, "配置文件")
            except ConfigError as exc:
                flash(str(exc), "error")
                return redirect(url_for("index"))
        if not raw_text:
            flash("请粘贴配置 JSON 或上传文件。", "error")
            return redirect(url_for("index"))
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            flash(f"JSON 解析失败：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）", "error")
            return redirect(url_for("index"))
        try:
            # 兼容两种载荷：纯配置，或 {config: {...}, archive: {...}}
            if isinstance(payload, dict) and "config" in payload:
                config_raw = payload["config"]
                archive_raw = payload.get("archive")
            else:
                config_raw = payload
                archive_raw = None
            cfg = build_config(config_raw)
            answers = {}
            if archive_raw is not None:
                answers, rebuilt = validate_archive(archive_raw, cfg)
                cfg = replace_followups(cfg, rebuilt)
        except ConfigError as exc:
            flash(f"配置/答卷校验失败：{exc}", "error")
            return redirect(url_for("index"))

        clear_session_keep_batch()
        save_config(cfg)
        session["answers"] = answers
        flash("核查任务已建立。", "ok")
        return redirect(url_for("index"))


    @app.route("/archive", methods=["POST"])
    def upload_archive():
        """在已有配置上单独上传/替换旧答卷。"""
        cfg, _ = require_state()
        if cfg is None:
            return redirect(url_for("index"))
        upload = request.files.get("archive_file")
        raw_text = request.form.get("archive_text", "").strip()
        if upload and upload.filename:
            try:
                raw_text = read_upload(upload, "答卷文件")
            except ConfigError as exc:
                flash(str(exc), "error")
                return redirect(url_for("index"))
        if not raw_text:
            flash("请粘贴答卷 JSON 或上传文件。", "error")
            return redirect(url_for("index"))
        try:
            archive_raw = json.loads(raw_text)
            answers, rebuilt = validate_archive(archive_raw, cfg)
        except json.JSONDecodeError as exc:
            flash(f"JSON 解析失败：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）", "error")
            return redirect(url_for("index"))
        except ConfigError as exc:
            flash(f"答卷校验失败：{exc}", "error")
            return redirect(url_for("index"))
        cfg = replace_followups(cfg, rebuilt)
        save_config(cfg)
        session["answers"] = answers
        session.pop("last_submission", None)
        flash("旧答卷已载入，跳题记录与答案已用于约束判定。", "ok")
        return redirect(url_for("index"))

    @app.route("/answer", methods=["POST"])
    def submit_answer():
        """补问任一未答筛查项；冲突则拒绝并保留提交前结果。"""
        cfg, answers = require_state()
        if cfg is None:
            return redirect(url_for("index"))
        name = request.form.get("item", "").strip()
        value_raw = request.form.get("value", "").strip().lower()
        if name not in cfg.items:
            flash(f"筛查项 {name} 不在配置内。", "error")
            return redirect(url_for("index"))
        if name in answers:
            flash(f"筛查项 {cfg.labels.get(name, name)} 已有答案，不能重复补问。", "error")
            return redirect(url_for("index"))
        if value_raw not in ("true", "false"):
            flash("请选择“是”或“否”。", "error")
            return redirect(url_for("index"))
        value = value_raw == "true"

        trial = dict(answers)
        trial[name] = value
        result = analyze(cfg, trial)
        if not result.satisfiable:
            session["last_submission"] = {
                "item": name,
                "value": value,
                "conflicts": [c.description for c in result.conflicting],
            }
            flash(
                f"该回答与旧档记录冲突，已拒绝并保留提交前结果："
                f"{cfg.labels.get(name, name)} = {'是' if value else '否'}。",
                "error",
            )
            return redirect(url_for("index"))

        session["answers"] = trial
        session.pop("last_submission", None)
        flash(f"已记录补问答案：{cfg.labels.get(name, name)} = {'是' if value else '否'}。", "ok")
        return redirect(url_for("index"))

    @app.route("/reset", methods=["POST"])
    def reset():
        clear_session_keep_batch()
        flash("会话已重置。", "ok")
        return redirect(url_for("index"))

    @app.route("/reset-answers", methods=["POST"])
    def reset_answers():
        cfg, _ = require_state()
        if cfg is None:
            return redirect(url_for("index"))
        session["answers"] = {}
        session.pop("last_submission", None)
        flash("已清空全部筛查项答案（保留配置与跳题记录）。", "ok")
        return redirect(url_for("index"))

    @app.template_filter("yn")
    def yn(value):
        return "是" if value else "否"

    # ------------------------------------------------------------------
    # 版号核查页：两版配置 + 整批答卷的版号核定
    # ------------------------------------------------------------------

    def vc_redirect():
        return redirect(url_for("version_check"))

    def read_json_field(text_field: str, file_field: str, label: str):
        """读取文本框或上传文件中的 JSON；未填写返回 None。"""
        raw_text = request.form.get(text_field, "").strip()
        upload = request.files.get(file_field)
        if upload and upload.filename:
            raw_text = read_upload(upload, label)
        if not raw_text:
            return None
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"{label} JSON 解析失败：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）"
            ) from exc

    def flash_blockers(prefix: str, exc: BatchError):
        shown = exc.blockers[:8]
        msg = prefix + "：" + "；".join(shown)
        if len(exc.blockers) > 8:
            msg += f"；…… 共 {len(exc.blockers)} 条"
        flash(msg, "error")

    def current_view():
        """加载会话指向的批次视图；批次丢失时清理会话并返回 None。"""
        batch_id = session.get("vc_batch_id")
        if not batch_id:
            return None
        try:
            return load_view(db_path, batch_id)
        except BatchNotFound:
            session.pop("vc_batch_id", None)
            flash("批次已不存在（可能服务重启或已重置），请重新导入。", "error")
            return None

    @app.route("/version-check")
    def version_check():
        view = current_view()
        focus = None
        focus_serial = request.args.get("serial", "").strip()
        if view is not None and focus_serial:
            focus = view.detail(focus_serial)
            if focus is None:
                flash(f"流水号 {focus_serial} 不在当前批次内。", "error")
        return render_template(
            "version_check.html",
            view=view,
            focus=focus,
            focus_serial=focus_serial,
            max_batch=MAX_BATCH,
            status_labels=STATUS_LABELS,
        )

    @app.route("/version-check/setup", methods=["POST"])
    def version_check_setup():
        """建立批次：两版配置 + 整批答卷（支持合并为一个 JSON）。"""
        try:
            old_raw = read_json_field("old_config_text", "old_config_file", "旧版配置")
            new_raw = read_json_field("new_config_text", "new_config_file", "新版配置")
            responses_raw = read_json_field("responses_text", "responses_file", "答卷批次")
        except ConfigError as exc:
            flash(str(exc), "error")
            return vc_redirect()

        # 合并形式：{"old_config": {...}, "new_config": {...}, "responses": [...]}
        if isinstance(responses_raw, dict) and "responses" in responses_raw:
            combined = responses_raw
            responses_raw = combined.get("responses")
            if old_raw is None and "old_config" in combined:
                old_raw = combined["old_config"]
            if new_raw is None and "new_config" in combined:
                new_raw = combined["new_config"]

        missing = []
        if old_raw is None:
            missing.append("旧版配置")
        if new_raw is None:
            missing.append("新版配置")
        if responses_raw is None:
            missing.append("答卷批次")
        if missing:
            flash("请提供" + "、".join(missing) + "。", "error")
            return vc_redirect()

        try:
            batch_id, result = create_batch(db_path, old_raw, new_raw, responses_raw)
        except ConfigError as exc:
            flash(f"配置校验失败：{exc}", "error")
            return vc_redirect()
        except BatchError as exc:
            if exc.blockers:
                flash_blockers(f"导入被拒绝，原批次与下载结果照旧（{exc}）", exc)
            else:
                flash(f"导入被拒绝：{exc}", "error")
            return vc_redirect()

        previous = session.get("vc_batch_id")
        session["vc_batch_id"] = batch_id
        if previous and previous != batch_id:
            drop_batch(db_path, previous)
        c = result.counts
        flash(
            f"批次已建立（状态 v1）：共 {c['total']} 份，确定新版 {c['new']}、"
            f"确定旧版 {c['old']}、待核 {c['pending']}。",
            "ok",
        )
        return vc_redirect()

    @app.route("/version-check/correct", methods=["POST"])
    def version_check_correct():
        """按流水号更正一份答卷；无解则整体回退并指出卡住的流水号。"""
        if not session.get("vc_batch_id"):
            flash("请先建立批次。", "error")
            return vc_redirect()
        try:
            payload = read_json_field("response_text", "response_file", "更正答卷")
        except ConfigError as exc:
            flash(str(exc), "error")
            return vc_redirect()
        if payload is None:
            flash("请粘贴或上传更正后的答卷 JSON。", "error")
            return vc_redirect()
        try:
            rec, result, state_version = correct_response(
                db_path, session["vc_batch_id"], payload
            )
        except BatchNotFound:
            session.pop("vc_batch_id", None)
            flash("批次已不存在，请重新导入。", "error")
            return vc_redirect()
        except BatchError as exc:
            if exc.blockers:
                flash_blockers(f"更正被拒绝，原数据与下载结果照旧（{exc}）", exc)
            else:
                flash(f"更正被拒绝：{exc}", "error")
            return vc_redirect()
        c = result.counts
        flash(
            f"流水号 {rec.serial} 已更正（状态 v{state_version}）：确定新版 {c['new']}、"
            f"确定旧版 {c['old']}、待核 {c['pending']}。",
            "ok",
        )
        return vc_redirect()

    @app.route("/version-check/download")
    def version_check_download():
        """下载入仓 JSON：只收在所有可行划分下都属新版的答卷。"""
        batch_id = session.get("vc_batch_id")
        if not batch_id:
            flash("请先建立批次。", "error")
            return vc_redirect()
        try:
            state_version, generated_at, entries = warehouse_snapshot(db_path, batch_id)
        except BatchNotFound:
            session.pop("vc_batch_id", None)
            flash("批次已不存在，请重新导入。", "error")
            return vc_redirect()
        doc = {
            "batch_id": batch_id,
            "state_version": state_version,
            "generated_at": generated_at,
            "criteria": "仅收在所有可行划分下都属新版的答卷",
            "count": len(entries),
            "responses": entries,
        }
        body = json.dumps(doc, ensure_ascii=False, indent=2)
        filename = f"warehouse_{batch_id[:8]}_v{state_version}.json"
        return Response(
            body,
            mimetype="application/json; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.route("/version-check/reset", methods=["POST"])
    def version_check_reset():
        batch_id = session.pop("vc_batch_id", None)
        if batch_id:
            drop_batch(db_path, batch_id)
        flash("版号核查批次已清空。", "ok")
        return vc_redirect()

    return app
