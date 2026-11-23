# 夜骑问卷版号核查

调查终端试用新版夜骑问卷后换回旧版，导出的答卷不带版号。本工具用**两版问卷配置**
与该终端导出的**答卷 JSON** 做版号核查：逐份判定与旧版/新版是否相容，再按
“新版答卷按流水号只能连成一段（也可以没有）”的规则求全部可行划分，给出
**确定旧版 / 确定新版 / 待核 / 无解** 结论，并支持按流水号单份更正与入仓下载。

## 判定规则

对每一份答卷，分别检查它与旧版、新版配置是否**相容**。相容要求同时满足：

1. 答卷的逐题展示/跳过记录与该版本的展示条件（只依赖此前题目的已答项）一致；
   前置题被跳过时，后续题也应当被跳过；
2. 记录为展示的题必须有已答项，记录为跳过的题不能有已答项；
3. 已答项取值符合题型（单选在选项内、数值在 min/max 内、是非可解释为布尔）；
4. 答卷不能出现该版本不存在的题号（例如旧版只有 4 题，答卷却含新版独有题 Q5）；
   答卷未覆盖该版本的题目按“未展示（跳过）”处理，因此天然的旧版答卷无需伪造新题记录。

版号划分规则：把批次按流水号排序后，**新版答卷必须构成一个连续区间（允许空区间）**，
区间内每份都与新版相容、区间外每份都与旧版相容。汇总全部可行划分：

| 结论 | 含义 |
| --- | --- |
| 确定新版 `confirmed_new` | 在每一个可行划分里都属于新版 |
| 确定旧版 `confirmed_old` | 在每一个可行划分里都属于旧版 |
| 待核 `pending` | 有的划分放新版、有的划分放旧版 |
| 无解 `unsolvable` | 不存在任何可行划分，页面指出卡住划分的流水号 |

- 每批最多 **2000** 份，每份必须有唯一流水号；
- **入仓下载只收“确定新版”**——即在所有可行划分下都属于新版的答卷；
- 不存在任何必须新版的答卷时，空区间（全旧）也是可行划分，此时没有答卷能成为确定新版。

## 更正语义（按流水号）

- 更正只**规范化并重判被更正的那一份**与两版的相容性，不逐份扫描、不改写其余答卷的数据；
- 重判后对整批重跑一次 O(n) 的区间求解，受影响的版号结论随更正一并更新，`revision + 1`；
- 若更正会使整批无解，更正被**拒绝**，页面给出卡住划分的流水号，原数据与既有下载结果不变；
- 下载与更正在同一把锁内互斥，每次下载都基于一个不可变快照，下载中的每份答卷与版号
  必然来自同一个已提交状态（响应头 `X-Source-Revision` 标明快照版本号）；更正期间可继续下载。

## 配置与数据格式

### 版本配置（旧版、新版各一份）

```json
{
  "version": "v1.0-old",
  "questions": [
    {"qid": "Q1", "qtype": "single", "options": ["A", "B", "C"]},
    {"qid": "Q2", "qtype": "bool", "condition": "Q1 == 'A'"},
    {"qid": "Q3", "qtype": "number", "min": 0, "max": 120, "condition": "Q2 == true"},
    {"qid": "Q4", "qtype": "bool"}
  ]
}
```

- 题型 `qtype`：`single`（单选，需 `options`）、`number`（可选 `min`/`max`）、`bool`；
- `condition` 为展示条件，缺省表示无条件展示；条件只能引用排在当前题之前的题号；
- 条件语法：`Q1 == 'A'`、`Q3 >= 30`、`Q2 != true`、是非题简写 `Q2`，
  以及 `AND / OR / NOT`（亦可用 `&& / || / !`、`与 / 或 / 非`）和括号。

### 答卷批次

顶层数组，或 `{"responses": [...]}`：

```json
{
  "responses": [
    {
      "serial": "R0001",
      "answers": {"Q1": "B", "Q4": true},
      "records": [
        {"qid": "Q1", "shown": true},
        {"qid": "Q2", "shown": false},
        {"qid": "Q3", "shown": false},
        {"qid": "Q4", "shown": true}
      ]
    }
  ]
}
```

也兼容 `shown_qids`（列出展示题，其余按跳过处理）的简写形式。

### 入仓下载

`GET /download` 返回 `warehouse_revN.json`，只含确定新版答卷，示例字段：

```json
{
  "exported_at": "…",
  "source_revision": 2,
  "new_version": "v2.0-new",
  "old_version": "v1.0-old",
  "rule": "仅收录在所有可行版号划分下都属于新版的答卷",
  "total_in_batch": 8,
  "accepted_count": 3,
  "responses": [ {"serial": "R0005", "answers": {"…": "…"}, "records": ["…"],
                  "verdict": "confirmed_new", "verdict_label": "确定新版"} ]
}
```

## 本地运行（Docker Compose）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

容器以 gunicorn **单 worker + 多线程**运行：批次状态保存在进程内存中，单 worker
保证状态全局一致，线程并发由应用内的锁保证原子提交。生产环境请通过环境变量
`SECRET_KEY` 覆盖默认密钥。

## 本地运行（无 Docker）

```bash
pip install -r requirements.txt
gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 4 'app.app:create_app()'
# 或开发模式：
FLASK_APP='app.app:create_app()' flask run --port 8000
```

## 测试与演示

```bash
# 引擎与路由测试（共 39 个；无 Flask 时 Web 用例自动跳过）
python3 -m unittest discover -s tests -v

# 命令行全流程演示：核定 -> 下载 -> 成功更正 -> 无解更正被拒 -> 无解批次定位
python3 -I scripts/demo.py

# Web 冒烟（先启动服务，再执行）
BASE=http://127.0.0.1:8000 bash scripts/smoke_web.sh
```

样例文件位于 `sample/`：

| 文件 | 说明 |
| --- | --- |
| `config_old.json` / `config_new.json` | 两版问卷配置（新版新增 Q5、调整跳题与数值上限） |
| `batch_solvable.json` | 可解批次：R0005–R0007 为确定新版，其余确定旧版 |
| `batch_unsolvable.json` | 无解批次：两个必须新版点被仅旧版的 R1002 隔开 |
| `correction_R0007_ok.json` | 成功更正样例（R0007 重判后仍确定新版，revision +1） |
| `correction_unsolvable.json` | 无解更正样例（截断唯一新版段，更正被拒绝并指出 R0006） |

## 目录结构

```
app/
  app.py                Flask 路由：配置/导入/查询/更正/下载/重置
  engine/
    conditions.py       条件词法、语法解析与按答案求值
    config.py           两版问卷配置模型与校验
    compat.py           单份答卷与单版配置的相容性判定
    solver.py           批次解析、相容复算、可行划分求解与结论分类
    store.py            带锁、带 revision 的原子提交快照存储与入仓导出
  templates/ static/    核查页
sample/                 配置、可解/无解批次、更正样例
scripts/                CLI 演示与 Web 冒烟
tests/                  条件、相容性/求解、Flask 路由测试
```

## 设计边界与未完成项

- 状态为**单进程内存存储**：按设计配合单 worker 的 docker compose 使用；多副本部署或
  重启后需要重新导入（不做跨进程持久化）。
- 更正接口要求提交**该流水号更正后的完整答卷**（整体替换该份），不做单字段补丁。
- 条件表达式支持常见比较与布尔组合，不支持算术表达式与跨题聚合函数。
- 流水号按字符串原样保序处理（上传数组顺序即流水号顺序），不做字典序/数值序重排。
