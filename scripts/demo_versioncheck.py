#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""版号核查命令行演示：不依赖 Flask，跑通“导入核定 -> 入仓下载 -> 更正 -> 无解更正”。

用法：
  python3 scripts/demo_versioncheck.py [sample/version_batch.json]
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.batchstore import (  # noqa: E402
    correct_response,
    create_batch,
    drop_batch,
    init_db,
    load_view,
    warehouse_snapshot,
)
from app.engine.versioncheck import STATUS_LABELS, BatchError  # noqa: E402


def show_view(view, title):
    print("=" * 72)
    print(f"{title}（已提交状态 v{view.state_version}）")
    print("-" * 72)
    c = view.classification.counts
    print(f"共 {c['total']} 份：确定新版 {c['new']}、确定旧版 {c['old']}、待核 {c['pending']}")
    if view.classification.must_new_span:
        lo, hi = view.classification.must_new_span
        blo, bhi = view.classification.new_segment_bounds
        print(f"必属新版区间 {lo} ~ {hi}；新版段可在 {blo} ~ {bhi} 之间")
    for row in view.rows:
        bits = f"旧版{'相容' if row.old_ok else '不相容':4s} 新版{'相容' if row.new_ok else '不相容'}"
        print(f"  {row.serial:>6}  {STATUS_LABELS[row.status]}  ({bits})")


def show_warehouse(db, batch_id):
    state_version, generated_at, entries = warehouse_snapshot(db, batch_id)
    print(f"入仓 JSON（状态 v{state_version}，{generated_at}）：{len(entries)} 份")
    for e in entries:
        print(f"  {e['serial']:>6}  version={e['version']}")
    return state_version, entries


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        root, "sample", "version_batch.json")
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)

    db = os.path.join(tempfile.mkdtemp(prefix="vc-demo-"), "demo.sqlite3")
    init_db(db)

    print("【1】导入整批答卷并核定版号")
    batch_id, _ = create_batch(
        db, payload["old_config"], payload["new_config"], payload["responses"]
    )
    show_view(load_view(db, batch_id), "导入后核定结果")

    print()
    print("【2】下载入仓 JSON（只收所有可行划分下都属新版的答卷）")
    show_warehouse(db, batch_id)

    print()
    print("【3】更正流水号 111（改为只与旧版相容）——只重判这一份")
    with open(os.path.join(root, "sample", "version_correction.json"),
              encoding="utf-8") as fh:
        correction = json.load(fh)
    rec, _, state_version = correct_response(db, batch_id, correction)
    print(f"流水号 {rec.serial} 已更正，进入状态 v{state_version}")
    view = load_view(db, batch_id)
    show_view(view, "更正后核定结果")
    note = view.detail("112")
    print(f"注意：流水号 112 未被更正，其版号随更正一并更新为 {STATUS_LABELS[note.status]}")
    committed_version, committed_entries = show_warehouse(db, batch_id)

    print()
    print("【4】无解更正：把 105 也改成只与旧版相容（将夹断新版连续段）")
    bad = {
        "serial": "105",
        "answers": {},
        "shown": {"Q1": True, "Q2": True},
    }
    try:
        correct_response(db, batch_id, bad)
        print("!! 预期外的成功")
    except BatchError as exc:
        print(f"更正被拒绝：{exc}")
        for b in exc.blockers:
            print(f"  卡住划分 -> {b}")
    view = load_view(db, batch_id)
    v2, _, e2 = warehouse_snapshot(db, batch_id)
    assert view.state_version == committed_version == v2, "状态号不应变化"
    assert [e["serial"] for e in e2] == [e["serial"] for e in committed_entries], \
        "入仓结果不应变化"
    print(f"原数据照旧：状态仍为 v{view.state_version}，入仓仍为 {len(e2)} 份")

    print()
    print("【5】无解导入：批次里混入一份与两版均不相容的答卷")
    bad_batch = {
        "old_config": payload["old_config"],
        "new_config": payload["new_config"],
        "responses": [
            {"serial": "201", "answers": {"S1": False, "S3": False},
             "shown": {"Q1": False, "Q2": True}},   # Q2 展示在两版下都不可能
            {"serial": "202", "answers": {},
             "shown": {"Q1": True, "Q2": False}},
        ],
    }
    try:
        create_batch(db, bad_batch["old_config"], bad_batch["new_config"],
                     bad_batch["responses"])
        print("!! 预期外的成功")
    except BatchError as exc:
        print(f"导入被拒绝：{exc}")
        for b in exc.blockers:
            print(f"  卡住划分 -> {b}")

    drop_batch(db, batch_id)
    print()
    print("演示完成。")


if __name__ == "__main__":
    main()
