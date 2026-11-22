# 巡查方式比较与值班复核应用

仅依赖 Python 3.14 标准库。应用代码在 `app/patrol/`，来源台账在 `../sources/`（永不回写），
运行期事件与不可变报告在 `runtime/`，可重放场景在 `../scenarios/`。

## 设计原则（对应业务约束）

1. **观察单位是 segment_version**。D6 v1 与拆分出的 D6-A/D6-B v2 是不同单位；
   父段被拆分后退役，其历史观察不回填新段，跨版本沿 `prior_segment` 世系对齐。
2. **只有 `selection_type=random` 的独立抽查带设计纳入概率**。漏检只能在随机窗口内
   用 Horvitz–Thompson 加权估计；`directed` 派工与 `substituted` 替代抽查只给描述性结论，
   不外推总体。替代/加查标记 `active_addition`，不能在事后当成随机发现。
3. **没有到场且无事前证据的记录保持未知**，不填阴性。`repaired` 是修后路面，
   配合已完工资料只登记为事前缺陷，不计入 open 阴性/阳性窗口。
4. **同一缺陷跨相邻段**（如 F20@D40+D41）建立缺陷组；两次复核只计一份现场成果，
   未单独观察的路面保留不确定性。
5. **原试点名单不可变**。所有运行期变化进入只追加事件日志（`events.log.jsonl`），
   报告快照不可变并带来源哈希与事件序号，可复核、可 diff、可完整重放。
6. **方法优劣只在有检查机会和证据的范围内下结论**；窗口外未抽中路段状态未知，
   下一轮计划强制为未报缺陷（clear）路段保留名额。

## 快速开始

```sh
# 全部测试（来源契约 + 应用逻辑），隔离模式
python3 -I app/run_tests.py

# 端到端演示：基线快照 -> 第2轮事件重放 -> 运行后快照 -> 报告差异 -> 去向追踪
python3 -I app/demo.py
```

## 分析人员：漏检与误报的可估计范围

```sh
python3 -I app/run.py --sources sources --runtime runtime analyze \
    --out runtime/analysis --report-id R1-baseline
```

产出 JSON / Markdown / 单文件 HTML 三份报告，包含：

- 人群、时期、观察单位与被排除单位（含拆分世系说明）；
- 两个漏检窗口（含/不含协议偏差路段的敏感性口径），HT 加权总数、SE、近似 95% 区间，
  以及窗口外未知路段数——区间不覆盖这部分不确定性；
- 误报/确认的描述性窗口（明确标注仅限被复核子集）；
- 现场登记的漏报证据清单（区分 random 与 directed）；
- 容量审计与可估计性边界说明。

## 值班人员：派工、改派、到场、修复、更正

所有写操作都是显式编号的只追加事件，重放结果确定。

```sh
# 容量（urgent_reserved 计入 places）
python3 -I app/run.py ... capacity --crew T1 --day 13 --places 8 --urgent-reserved 3 --event-id E02

# 任一方式的紧急线索派工（延误影响维修时限）
python3 -I app/run.py ... dispatch --report P4 --crew T1 --day 13 --event-id E12

# 关闭路段 + 关闭命中计划时的可执行改派（自动选同区组替代段，原名单不变）
python3 -I app/run.py ... close --segment D11 --day 13 --reason 施工封闭 --event-id E10
python3 -I app/run.py ... substitute --draw V11 --day 13 --reroute-closed --event-id E11

# 到场（repaired 必须先有完工资料，否则拒绝）
python3 -I app/run.py ... arrival --draw R2-U-P4 --day 14 --physical-state repaired --event-id E20

# 完工、相邻同缺陷组合并（不重复计派工成果）、证据更正（追加不改原始记录）
python3 -I app/run.py ... repair --job W4 --segment D4 --defect F2 --day 13 --event-id E14
python3 -I app/run.py ... merge --group-label F20@D40+D41 --merged-reports P40 --kept-report O41 --day 14 --event-id E22
python3 -I app/run.py ... correct --report P18 --new-status confirmed_open --day 14 --note 现场确认 --event-id E25
```

## 下一轮计划与独立抽查名额

```sh
python3 -I app/run.py ... plan --round 2 --day 15 --crews T1,T2 \
    --negative-fraction 0.5 --event-id E30
```

- 至少为 clear（未报缺陷）路段保留名额，随机名额带纳入概率，可继续估计漏检；
- 关闭段与已退役父段不进抽取池，拆分新段自动成为合格单位；
- 派班受班组容量约束，超容量直接报错而不是静默超额；
- 计划生成后若有关闭段命中，用 `substitute --reroute-closed` 输出替代段，
  替代抽查为 `substituted`、无概率、标记主动加查。

## 报告复核与线索追踪

```sh
# 不可变快照（baseline 要求事件日志为空）
python3 -I app/run.py ... snapshot --baseline --report-id R1-baseline --title 第1轮报告 --out runtime/reports
python3 -I app/run.py ... snapshot --report-id R2-round2 --title 第2轮快照 --out runtime/reports

# 两份已使用报告的字段级差异
python3 -I app/run.py ... report-diff --left R1-baseline --right R2-round2

# 某条线索/抽查为何未执行：repaired | merged | quota_adjusted | pending | closed | none
python3 -I app/run.py ... trace --key P4
python3 -I app/run.py ... backlog
```

## 模块结构

| 模块 | 职责 |
| --- | --- |
| `model.py` | 不可变领域记录与状态枚举 |
| `ingest.py` | 七张台账读取、表头/引用/概率/repaired 事前证据校验 |
| `domain.py` | 相邻判定、缺陷组、线索状态、抽查证据分级、世系与容量审计 |
| `estimation.py` | HT 加权、Wilson 区间、可估计窗口与描述性窗口 |
| `workflow.py` | 派工/改派/关闭/到场/修复/更正/计划，事件重放与合并数据集 |
| `store.py` | 只追加事件日志、不可变报告快照与 diff |
| `report.py` | JSON/Markdown/单文件 HTML 渲染 |
| `cli.py` | 命令入口；`run.py` 以隔离模式启动，数据目录不进 sys.path |
