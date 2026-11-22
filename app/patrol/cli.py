"""命令行入口：分析、报告快照、值班工作流、计划与追踪。

示例：
  python -m patrol.cli analyze --sources sources --out runtime/reports
  python -m patrol.cli replay --sources sources --runtime runtime --scenario scenarios/round2.json
  python -m patrol.cli trace --sources sources --runtime runtime --key P4
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .domain import (
    audit_capacity,
    build_defect_groups,
    build_draw_evidence,
    classify_leads,
    pending_urgent_leads,
)
from .estimation import build_comparison
from .ingest import load_dataset
from .report import build_report_payload, render_markdown, write_report_files
from .store import Store, StoreError
from .workflow import Workflow


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_analyze(args: argparse.Namespace) -> int:
    ds = load_dataset(args.sources)
    groups = build_defect_groups(ds)
    leads = classify_leads(ds, groups)
    evidence = build_draw_evidence(ds)
    comparison = build_comparison(ds, evidence, leads)
    store = Store(args.runtime) if Path(args.runtime).exists() else None
    payload = build_report_payload(
        ds, comparison, leads, audit_capacity(ds), store,
        report_id=args.report_id, title=args.title,
    )
    paths = write_report_files(payload, args.out, args.report_id)
    # analyze 是只读分析，不写入不可变快照存储；快照由 snapshot 命令显式生成
    summary = {
        "report_id": args.report_id,
        "files": {k: str(v) for k, v in paths.items()},
        "windows": [
            {
                "label": w.label, "n_sampled": w.n_sampled,
                "observed_misses": w.observed_misses,
                "estimated_rate": w.estimated_rate,
                "rate_ci95": list(w.rate_ci95),
                "unknown_segments": w.unknown_segments,
            }
            for w in comparison.windows
        ],
        "pending_urgent": [
            {"report": l.report, "segment": l.segment, "status": l.status.value,
             "reason": l.reason.value}
            for l in pending_urgent_leads(leads)
        ],
    }
    _print(summary)
    return 0


def _load_workflow(args: argparse.Namespace) -> tuple[Workflow, Store]:
    ds = load_dataset(args.sources)
    store = Store(args.runtime)
    return Workflow(ds, store), store


def cmd_replay(args: argparse.Namespace) -> int:
    workflow, store = _load_workflow(args)
    scenario = json.loads(Path(args.scenario).read_text(encoding="utf-8"))
    applied = []
    # 场景业务键 -> 方法参数名（避免 round 等内建名冲突）
    ARG_ALIASES = {
        "start_round": {"round": "round_no"},
        "plan": {"round": "round_no"},
    }
    for step in scenario["events"]:
        handler = getattr(workflow, step["command"])
        kwargs = dict(step["args"])
        for old, new in ARG_ALIASES.get(step["command"], {}).items():
            if old in kwargs:
                kwargs[new] = kwargs.pop(old)
        kwargs["event_id"] = step["id"]
        try:
            record = handler(**kwargs)
        except TypeError as exc:
            raise StoreError(
                f"场景事件 {step['id']}（{step['command']}）参数不匹配：{exc}"
            ) from exc
        applied.append({"id": record["id"], "type": record["type"], "seq": record["seq"]})
    _print({"replayed": len(applied), "events": applied,
            "event_log": str(store.events_path)})
    return 0


def cmd_dispatch(args: argparse.Namespace) -> int:
    workflow, _ = _load_workflow(args)
    record = workflow.dispatch_urgent(args.report, args.crew, args.day, args.event_id)
    _print(record)
    return 0


def cmd_substitute(args: argparse.Namespace) -> int:
    workflow, _ = _load_workflow(args)
    if args.reroute_closed:
        record = workflow.substitute(draw=args.draw, day=args.day,
                                     event_id=args.event_id, reroute_closed=True)
    else:
        if not args.to_segment:
            raise StoreError("普通改派需要 --to-segment")
        record = workflow.substitute(draw=args.draw, day=args.day,
                                     event_id=args.event_id,
                                     to_segment=args.to_segment)
    _print(record)
    return 0


def cmd_arrival(args: argparse.Namespace) -> int:
    workflow, _ = _load_workflow(args)
    record = workflow.record_arrival(
        args.draw, args.day, args.defect or "", args.physical_state,
        args.event_id, note=args.note or "",
    )
    _print(record)
    return 0


def cmd_repair(args: argparse.Namespace) -> int:
    workflow, _ = _load_workflow(args)
    record = workflow.complete_repair(args.job, args.segment, args.defect,
                                      args.day, args.event_id)
    _print(record)
    return 0


def cmd_correct(args: argparse.Namespace) -> int:
    workflow, _ = _load_workflow(args)
    record = workflow.correct_evidence(
        args.report, args.new_status, args.day, args.note,
        args.event_id, new_reason=args.new_reason,
    )
    _print(record)
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    workflow, _ = _load_workflow(args)
    record = workflow.confirm_merge(
        args.group_label, args.merged_reports.split(","), args.kept_report,
        args.day, args.event_id,
    )
    _print(record)
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    workflow, _ = _load_workflow(args)
    plan = workflow.plan_next_round(
        round_no=args.round, day=args.day, crews=args.crews.split(","),
        negative_fraction=args.negative_fraction,
        event_id=args.event_id or "",
    )
    _print(plan)
    return 0


def cmd_capacity(args: argparse.Namespace) -> int:
    workflow, _ = _load_workflow(args)
    record = workflow.set_capacity(args.crew, args.day, args.places,
                                   args.urgent_reserved, args.event_id)
    _print(record)
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    workflow, _ = _load_workflow(args)
    record = workflow.close_segment(
        args.segment, args.day, args.reason or "", args.event_id,
        reopen=args.reopen,
    )
    _print(record)
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    workflow, _ = _load_workflow(args)
    _print(workflow.trace(args.key))
    return 0


def cmd_backlog(args: argparse.Namespace) -> int:
    workflow, _ = _load_workflow(args)
    _print(workflow.backlog())
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    ds_raw = load_dataset(args.sources)
    store = Store(args.runtime)
    if args.baseline:
        # 基线：仅来源台账，事件序号必须为 0
        if store.load_events():
            raise StoreError(
                "baseline 快照只能在空事件日志上生成；当前日志已有事件，"
                "请用独立 runtime 目录或改用普通 snapshot"
            )
        ds = ds_raw
        workflow = None
    else:
        workflow = Workflow(ds_raw, store)
        ds = workflow.to_combined_dataset()
    groups = build_defect_groups(ds)
    leads = classify_leads(ds, groups)
    evidence = build_draw_evidence(ds)
    comparison = build_comparison(ds, evidence, leads)
    payload = build_report_payload(
        ds, comparison, leads, audit_capacity(ds), store,
        report_id=args.report_id, title=args.title, workflow=workflow,
    )
    store.save_report(args.report_id, payload)
    paths = write_report_files(payload, args.out, args.report_id)
    _print({"report_id": args.report_id,
            "files": {k: str(v) for k, v in paths.items()},
            "event_seq": payload["created_after_event_seq"]})
    return 0


def cmd_report_diff(args: argparse.Namespace) -> int:
    _, store = _load_workflow(args)
    _print(store.diff_reports(args.left, args.right))
    return 0


def cmd_list_reports(args: argparse.Namespace) -> int:
    _, store = _load_workflow(args)
    _print(store.list_reports())
    return 0


def cmd_markdown(args: argparse.Namespace) -> int:
    _, store = _load_workflow(args)
    print(render_markdown(store.load_report(args.report_id)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="patrol", description="道路巡查试点分析应用")
    parser.add_argument("--sources", default="sources", help="来源台账目录")
    parser.add_argument("--runtime", default="runtime", help="运行期事件/报告目录")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("analyze", help="只读分析当前台账并产出报告文件")
    p.add_argument("--out", default="runtime/reports")
    p.add_argument("--report-id", default="R1-baseline")
    p.add_argument("--title", default="第1轮巡查方式比较报告")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("replay", help="按场景文件顺序重放事件")
    p.add_argument("--scenario", required=True)
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("dispatch", help="紧急线索派工")
    p.add_argument("--report", required=True)
    p.add_argument("--crew", required=True)
    p.add_argument("--day", type=int, required=True)
    p.add_argument("--event-id", required=True)
    p.set_defaults(func=cmd_dispatch)

    p = sub.add_parser("substitute", help="抽查名额改派（可对关闭路段自动重派）")
    p.add_argument("--draw", required=True)
    p.add_argument("--to-segment", default="")
    p.add_argument("--day", type=int, required=True)
    p.add_argument("--event-id", required=True)
    p.add_argument("--reroute-closed", action="store_true",
                   help="关闭路段命中两个计划时，自动选取同区组替代段")
    p.set_defaults(func=cmd_substitute)

    p = sub.add_parser("arrival", help="登记到场结果")
    p.add_argument("--draw", required=True)
    p.add_argument("--day", type=int, required=True)
    p.add_argument("--defect", default="")
    p.add_argument("--physical-state", required=True,
                   choices=["open", "repaired", "closed"])
    p.add_argument("--note", default="")
    p.add_argument("--event-id", required=True)
    p.set_defaults(func=cmd_arrival)

    p = sub.add_parser("repair", help="登记完工修复")
    p.add_argument("--job", required=True)
    p.add_argument("--segment", required=True)
    p.add_argument("--defect", required=True)
    p.add_argument("--day", type=int, required=True)
    p.add_argument("--event-id", required=True)
    p.set_defaults(func=cmd_repair)

    p = sub.add_parser("correct", help="证据更正（追加，不改原始记录）")
    p.add_argument("--report", required=True)
    p.add_argument("--new-status", required=True)
    p.add_argument("--new-reason", default=None)
    p.add_argument("--day", type=int, required=True)
    p.add_argument("--note", required=True)
    p.add_argument("--event-id", required=True)
    p.set_defaults(func=cmd_correct)

    p = sub.add_parser("merge", help="确认相邻段同缺陷组，合并派工成果")
    p.add_argument("--group-label", required=True)
    p.add_argument("--merged-reports", required=True, help="逗号分隔的 report 编号")
    p.add_argument("--kept-report", required=True)
    p.add_argument("--day", type=int, required=True)
    p.add_argument("--event-id", required=True)
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("plan", help="生成（可选落盘）下一轮计划，含 clear 预留名额")
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--day", type=int, required=True)
    p.add_argument("--crews", required=True)
    p.add_argument("--negative-fraction", type=float, default=0.5)
    p.add_argument("--event-id", default="")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("capacity", help="设置班组某日容量")
    p.add_argument("--crew", required=True)
    p.add_argument("--day", type=int, required=True)
    p.add_argument("--places", type=int, required=True)
    p.add_argument("--urgent-reserved", type=int, required=True)
    p.add_argument("--event-id", required=True)
    p.set_defaults(func=cmd_capacity)

    p = sub.add_parser("close", help="关闭/重开路段")
    p.add_argument("--segment", required=True)
    p.add_argument("--day", type=int, required=True)
    p.add_argument("--reason", default="")
    p.add_argument("--reopen", action="store_true")
    p.add_argument("--event-id", required=True)
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("trace", help="查询线索/抽查去向（修复/合并/名额/待核实/关闭）")
    p.add_argument("--key", required=True)
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("backlog", help="现行维修待办与关闭/修复情况")
    p.set_defaults(func=cmd_backlog)

    p = sub.add_parser("snapshot", help="把运行期事件合并后生成新的不可变报告快照")
    p.add_argument("--out", default="runtime/reports")
    p.add_argument("--report-id", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--baseline", action="store_true",
                   help="基线模式：仅用来源台账，要求事件日志为空（seq=0）")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("report-diff", help="比较两份已使用报告快照的字段变化")
    p.add_argument("--left", required=True)
    p.add_argument("--right", required=True)
    p.set_defaults(func=cmd_report_diff)

    p = sub.add_parser("list-reports", help="列出报告快照")
    p.set_defaults(func=cmd_list_reports)

    p = sub.add_parser("markdown", help="输出某份报告的 Markdown")
    p.add_argument("--report-id", required=True)
    p.set_defaults(func=cmd_markdown)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except StoreError as exc:
        print(f"工作流错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
