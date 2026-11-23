#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行演示：不依赖 Flask，跑通“两版配置 -> 导入批次 -> 版号核定
-> 单份更正（成功/无解）-> 入仓导出”全流程。

用法：
  python3 -I scripts/demo.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.engine import (
    BatchStore,
    StoreError,
    VERDICT_LABELS,
    build_pair,
)

SAMPLE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sample")


def load(name):
    with open(os.path.join(SAMPLE_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


def show_snapshot(store: BatchStore, title: str) -> None:
    snap = store.get()
    print("=" * 72)
    print(title, f"(revision={snap.revision})")
    print("-" * 72)
    b = snap.batch
    if not b.solvable:
        print("整批无解，卡住划分的流水号：" + "、".join(b.stuck_serials))
        return
    print(f"共 {b.count} 份：确定新版 {b.count_confirmed_new} / "
          f"确定旧版 {b.count_confirmed_old} / 待核 {b.count_pending}")
    for r in snap.responses:
        print(f"  {r.serial:8s} 旧版={'相容' if r.compat_old else '不相容'}  "
              f"新版={'相容' if r.compat_new else '不相容'}  => {VERDICT_LABELS[r.verdict]}")


def main() -> int:
    pair = build_pair(load("config_old.json"), load("config_new.json"))
    store = BatchStore()

    store.load_batch(load("batch_solvable.json"), pair)
    show_snapshot(store, "1) 导入可解批次并核定版号")

    print("\n2) 下载入仓 JSON（只收所有可行划分下都属新版的答卷）")
    payload = store.export_payload()
    print(f"   收录 {payload['accepted_count']} 份："
          + "、".join(r["serial"] for r in payload["responses"]))

    print("\n3) 成功更正 R0007（只重判这一份，其余答卷不扫描不改写）")
    snap = store.correct("R0007", load("correction_R0007_ok.json"))
    print(f"   已提交新状态 revision={snap.revision}")
    target = next(r for r in snap.responses if r.serial == "R0007")
    print(f"   R0007 当前结论：{VERDICT_LABELS[target.verdict]}")

    print("\n4) 无解更正：把 R0006 改成仅旧版相容，截断唯一的新版段")
    before_rev = store.get().revision
    try:
        store.correct("R0006", load("correction_unsolvable.json"))
    except StoreError as exc:
        print("   更正被拒绝：" + str(exc))
    assert store.get().revision == before_rev, "拒绝后 revision 不应变化"
    print(f"   原状态保持不变（revision 仍为 {before_rev}），下载结果照旧")

    print("\n5) 导入天生无解的批次（两个必须新版点被仅旧版答卷隔开）")
    store.load_batch(load("batch_unsolvable.json"), pair)
    snap = store.get()
    assert not snap.batch.solvable
    print("   卡住划分的流水号：" + "、".join(snap.batch.stuck_serials))

    print("\n全部演示完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
