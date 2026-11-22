# 道路巡查试点的漏检估计与复核名额

市政道路巡查分组、独立抽查及现场养护的合成资料。 所有标识、金额、地名和观察值均为合成数据。

## 来源资料

- `sources/trial_assignment.csv`
- `sources/survey_reports.csv`
- `sources/verification_draws.csv`
- `sources/field_outcomes.csv`
- `sources/works.csv`
- `sources/dispatch_updates.csv`
- `sources/crew_capacity.csv`

segment 是片段标识，prior_segment 不为空表示由旧段分开后启用的新段。trial_assignment 记录路段所属试验区组和首次纳入日期。survey_reports 的 candidate 可为空；相邻路段可就同一 candidate 分别提交记录。

verification_draws 包含独立随机抽查和日常线索派工，只有 random 有设计纳入概率。field_outcomes 的 physical_state 描述现场所见，repaired 表示调查前已修复，不能作为修复前的阴性观察。

dispatch_updates 的 substitution 只说明现场安排改变，抽样组未给替代对象附原抽样概率。crew_capacity 的 urgent_reserved 计入 places 总名额。

## 本地检查

使用 Python 3.14，仅需标准库。在项目目录执行以下命令检查输入表结构、事件引用和基础记录覆盖。

```sh
python -m unittest discover -s tests -v
```

## 分析应用

可复跑的比较与值班应用位于 `app/`（仅标准库），支持：随机窗口内的漏检 HT 加权估计、
描述性误报窗口、相邻缺陷组合并、拆分世系、紧急派工/改派/关闭重派、含未报缺陷预留名额的
下一轮计划、只追加事件日志、不可变报告快照与差异、线索去向追踪。

```sh
python3 -I app/run_tests.py           # 全部测试（隔离模式）
python3 -I app/demo.py                # 端到端可复跑演示
```

详见 `app/README.md`。运行期产物写入 `runtime/`（事件日志与报告快照），
可重放场景在 `scenarios/round2.json`；`sources/` 台账在任何流程中都不被修改。
