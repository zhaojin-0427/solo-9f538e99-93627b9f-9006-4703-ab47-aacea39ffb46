#!/usr/bin/env bash
# 容器/本地冒烟：启动 Web 后跑通“两版配置 -> 批次核定 -> 查询 -> 更正 -> 无解更正 -> 下载”。
# 依赖 curl 与可用的 Flask 环境（容器内已具备）。
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
jar="$(mktemp -t cookies.XXXXXX)"
trap 'rm -f "$jar"' EXIT

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OLD="$(python3 -c 'import json;print(json.dumps(json.load(open("'$ROOT'/sample/config_old.json"))))')"
NEW="$(python3 -c 'import json;print(json.dumps(json.load(open("'$ROOT'/sample/config_new.json"))))')"
BATCH="$(python3 -c 'import json;print(json.dumps(json.load(open("'$ROOT'/sample/batch_solvable.json"))))')"
BADFIX="$(python3 -c 'import json;print(json.dumps(json.load(open("'$ROOT'/sample/correction_unsolvable.json"))))')"
OKFIX="$(python3 -c 'import json;print(json.dumps(json.load(open("'$ROOT'/sample/correction_R0007_ok.json"))))')"

post() { # url  data...
  local url="$1"; shift
  curl -sS -b "$jar" -c "$jar" -o /dev/null -w '%{http_code}' \
       --data-urlencode "$1" ${2:+--data-urlencode "$2"} ${3:+--data-urlencode "$3"} \
       ${4:+--data-urlencode "$4"} "$BASE$url"
}

get_page() { # path
  curl -sS -b "$jar" -c "$jar" "$BASE$1"
}

echo "1) 建立两版配置并导入批次"
code=$(curl -sS -b "$jar" -c "$jar" -o /dev/null -w '%{http_code}' \
  --data-urlencode "old_config_text=$OLD" \
  --data-urlencode "new_config_text=$NEW" \
  --data-urlencode "batch_text=$BATCH" "$BASE/setup")
[ "$code" = "302" ] && echo "   OK ($code 重定向)" || { echo "   FAIL $code"; exit 1; }

page="$(get_page /)"
echo "$page" | grep -q "确定新版" && echo "2) 版号核定展示 OK" || { echo "2) FAIL"; exit 1; }
echo "$page" | grep -q "revision=1" && echo "   状态版本号 OK"

echo "3) 按流水号查询 R0005（确定新版）"
page="$(get_page "/?serial=R0005")"
echo "$page" | grep -q "确定新版" && echo "   查询 OK" || { echo "3) FAIL"; exit 1; }

echo "4) 成功更正 R0007（revision 应 +1）"
code=$(post /correct "serial=R0007" "corrected_text=$OKFIX")
[ "$code" = "302" ] || { echo "4) FAIL $code"; exit 1; }
page="$(get_page /)"
echo "$page" | grep -q "revision=2" && echo "   revision 递增 OK" || { echo "4) FAIL"; exit 1; }

echo "5) 无解更正 R0006（应拒绝并指出卡住流水号，revision 保持 2）"
code=$(post /correct "serial=R0006" "corrected_text=$BADFIX")
[ "$code" = "302" ] || { echo "5) FAIL $code"; exit 1; }
page="$(get_page /)"
echo "$page" | grep -q "更正被拒绝" && echo "   拒绝提示 OK" || { echo "5) FAIL"; exit 1; }
echo "$page" | grep -q "R0006" && echo "   卡住流水号 OK" || { echo "5) FAIL"; exit 1; }
echo "$page" | grep -q "revision=2" && echo "   原状态照旧 OK" || { echo "5) FAIL"; exit 1; }

echo "6) 下载入仓 JSON（仅确定新版，且带 X-Source-Revision）"
curl -sS -b "$jar" -D - "$BASE/download" -o /tmp/warehouse_smoke.json > /tmp/wh_headers
grep -qi "X-Source-Revision: 2" /tmp/wh_headers && echo "   快照版本号头 OK" || { echo "6) FAIL"; exit 1; }
python3 -c '
import json
p = json.load(open("/tmp/warehouse_smoke.json"))
assert p["source_revision"] == 2
assert {r["serial"] for r in p["responses"]} == {"R0005", "R0006", "R0007"}
assert all(r["verdict"] == "confirmed_new" for r in p["responses"])
print("   入仓内容 OK（%d 份）" % p["accepted_count"])
'
rm -f /tmp/warehouse_smoke.json

echo "全部冒烟通过。"
