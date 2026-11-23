#!/usr/bin/env bash
# 版号核查页冒烟：启动 Web 后跑“导入核定 -> 查询 -> 下载 -> 更正 -> 无解更正”。
# 依赖 curl + 可用的 Flask 环境（容器内已具备）。
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
SAMPLE="${SAMPLE:-sample/version_batch.json}"
jar="$(mktemp -t cookies.XXXXXX)"
tmp="$(mktemp -d -t vc-smoke.XXXXXX)"
trap 'rm -f "$jar"; rm -rf "$tmp"' EXIT

page() { curl -sS -b "$jar" -c "$jar" "$@"; }

echo "1) 建立批次（合并载荷：两版配置 + 12 份答卷）"
code=$(curl -sS -o /dev/null -w '%{http_code}' -b "$jar" -c "$jar" -L \
     --data-urlencode "responses_text@$SAMPLE" "$BASE/version-check/setup")
[ "$code" = "200" ] && echo "   OK ($code)" || { echo "   FAIL $code"; exit 1; }

out="$(page "$BASE/version-check")"
echo "$out" | grep -q "已提交状态 v1" && echo "2) 状态 v1 展示 OK" || { echo "2) FAIL"; exit 1; }
echo "$out" | grep -q "确定新版" && echo "   三态统计 OK"

echo "3) 按流水号查询 111（应为待核）"
out="$(page "$BASE/version-check?serial=111")"
echo "$out" | grep -q "待核" || { echo "   FAIL"; exit 1; }
echo "   OK"

echo "4) 下载入仓 JSON（应只含 104-110 共 7 份）"
page "$BASE/version-check/download" -o "$tmp/wh1.json"
python3 - "$tmp/wh1.json" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
assert doc["state_version"] == 1, doc["state_version"]
serials = [e["serial"] for e in doc["responses"]]
assert serials == ["104","105","106","107","108","109","110"], serials
assert all(e["version"] == "new" for e in doc["responses"])
print("   OK: 7 份确定新版，状态 v1")
PY

echo "5) 更正 111 为只与旧版相容（状态应进 v2，112 随之变确定旧版）"
code=$(curl -sS -o /dev/null -w '%{http_code}' -b "$jar" -c "$jar" -L \
     --data-urlencode 'response_text={"serial":"111","answers":{"S1":true},"shown":{"Q1":true,"Q2":true}}' \
     "$BASE/version-check/correct")
[ "$code" = "200" ] || { echo "   FAIL $code"; exit 1; }
out="$(page "$BASE/version-check?serial=112")"
echo "$out" | grep -q "已提交状态 v2" && echo "   状态 v2 OK" || { echo "   FAIL"; exit 1; }
echo "$out" | grep -q "确定旧版" && echo "   112 级联更新 OK" || { echo "   FAIL"; exit 1; }

echo "6) 无解更正：105 改为必旧（夹断 104~110），应被拒绝并指出流水号"
# 跟随重定向的那一次响应即含 flash（再次 GET 会消费掉），直接检查该响应
out="$(curl -sS -b "$jar" -c "$jar" -L \
     --data-urlencode 'response_text={"serial":"105","answers":{},"shown":{"Q1":true,"Q2":true}}' \
     "$BASE/version-check/correct")"
echo "$out" | grep -q "更正被拒绝" || { echo "   FAIL: 未见拒绝提示"; exit 1; }
echo "$out" | grep -q "105" && echo "$out" | grep -q "夹在必属新版" \
  && echo "   卡住流水号提示 OK" || { echo "   FAIL: 未指出流水号"; exit 1; }

echo "7) 下载结果照旧（仍 v2、7 份）"
page "$BASE/version-check/download" -o "$tmp/wh2.json"
python3 - "$tmp/wh2.json" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
assert doc["state_version"] == 2, doc["state_version"]
assert doc["count"] == 7, doc["count"]
print("   OK: 状态 v2，入仓 7 份，与更正前一致")
PY

echo "全部冒烟通过。"
