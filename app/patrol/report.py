"""比较报告装配与渲染：JSON 快照、Markdown、单文件 HTML。"""

from __future__ import annotations

import html
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path

from .domain import CapacityAudit, Lead, build_round_one_cohort
from .estimation import ComparisonReport
from .model import Dataset
from .store import Store
from .workflow import Workflow


def _plain(obj):
    if is_dataclass(obj):
        return _plain(asdict(obj))
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if isinstance(obj, float) and obj != obj:  # NaN
        return None
    return obj


def _pct(value: float | None) -> str:
    if value is None or value != value:  # NaN
        return "不可估计"
    return f"{value * 100:.1f}%"


def _num(value, digits=2) -> str:
    if value is None or value != value:
        return "—"
    return f"{value:.{digits}f}"


def build_report_payload(
    ds: Dataset,
    comparison: ComparisonReport,
    leads: list[Lead],
    capacity_audit: list[CapacityAudit],
    store: Store | None,
    report_id: str,
    title: str,
    workflow: Workflow | None = None,
) -> dict:
    cohort = build_round_one_cohort(ds)
    pending_urgent = [
        {
            "report": lead.report, "segment": lead.segment,
            "candidate": lead.candidate, "severity": lead.severity,
            "status": lead.status.value, "reason": lead.reason.value,
            "caveat": lead.caveat,
        }
        for lead in leads
        if lead.severity == "urgent" and not lead.executed
    ]
    payload = {
        "report_id": report_id,
        "title": title,
        "cohort": {
            "name": cohort.name,
            "period": cohort.period_label,
            "unit": cohort.unit,
            "included_segments": list(cohort.segments),
            "excluded": [{"segment": s, "reason": r} for s, r in cohort.excluded],
        },
        "comparison": _plain(comparison),
        "capacity_audit": [_plain(a) for a in capacity_audit],
        "pending_urgent_leads": pending_urgent,
        "backlog": workflow.backlog() if workflow else None,
        "estimability_notes": comparison.estimability_notes,
        "source_hashes": dict(ds.source_hashes),
        "event_log_hash": store.log_hash() if store else None,
        "created_after_event_seq": len(store.load_events()) if store else 0,
    }
    return payload


def render_markdown(payload: dict) -> str:
    lines: list[str] = []
    lines.append(f"# {payload['title']}（{payload['report_id']}）")
    cohort = payload["cohort"]
    lines.append("")
    lines.append("## 1. 人群、时期与观察单位")
    lines.append(f"- 人群：{cohort['name']}，纳入 {len(cohort['included_segments'])} 个 v1 路段")
    lines.append(f"- 时期：{cohort['period']}")
    lines.append(f"- 观察单位：{cohort['unit']}")
    for item in cohort["excluded"]:
        lines.append(f"- 排除 {item['segment']}：{item['reason']}")

    comp = payload["comparison"]
    lines.append("")
    lines.append("## 2. 漏检可估计范围（仅随机抽查窗口）")
    for window in comp["windows"]:
        lines.append(f"### {window['label']}")
        lines.append(f"- 抽中路段 {window['n_sampled']} 条；open 观察到漏报缺陷 "
                     f"{window['observed_misses']} 条；另有事前证据型 repaired "
                     f"{window['prior_evidence_misses']} 条（不计入 open 窗口）")
        lines.append(f"- 目标总体（未报缺陷 clear 路段）{window['population_size']} 条；"
                     f"窗口覆盖 {window['covered_segments']} 条，"
                     f"窗口外未知 {window['unknown_segments']} 条")
        lines.append(f"- HT 加权漏检缺陷总数估计：{_num(window['ht_total_misses'], 1)}"
                     f"（SE {_num(window['ht_se'], 1)}）")
        lines.append(f"- 估计漏检率：{_pct(window['estimated_rate'])}，"
                     f"近似95%区间 [{_pct(window['rate_ci95'][0])}, {_pct(window['rate_ci95'][1])}]")
        for method, row in window["method_breakdown"].items():
            lo, hi = row["wilson_observed_rate"]
            lines.append(
                f"  - {method}：抽 {row['sampled']}，见漏报 {row['observed_misses']}，"
                f"加权总数 {_num(row['weighted_total_misses'], 1)}，"
                f"窗口观察率 Wilson 区间 [{_pct(lo)}, {_pct(hi)}]，"
                f"clear 总体 {row['clear_population']}"
            )
        lines.append(f"- 涉及抽查：{', '.join(window['draw_ids'])}")

    lines.append("")
    lines.append("## 3. 误报与确认（描述性，仅限被复核子集，不外推）")
    for key, window in comp["descriptive"].items():
        rate = window["not_found_rate"]
        lo, hi = window["not_found_ci95"]
        lines.append(
            f"- {window['label']} [{key}]：线索 {window['n']}，确认 "
            f"{window['confirmed']}，未见 {window['not_found']}，修前已修 "
            f"{window['repaired_before']}，证据不足 {window['inconclusive']}，"
            f"待核实 {window['pending']}；已解决子集未见率 {_pct(rate)}"
            f"（Wilson [{_pct(lo)}, {_pct(hi)}]）"
        )

    lines.append("")
    lines.append("## 4. 现场登记的漏报证据")
    for miss in comp["observed_miss_outcomes"]:
        flag = "（协议偏差路段）" if miss["deviation"] else ""
        lines.append(f"- {miss['result']}：{miss['segment']}（{miss['method']}）由 "
                     f"{miss['draw_type']} 抽查 {miss['draw']} 发现 {miss['defect']}{flag}")

    lines.append("")
    lines.append("## 5. 方式比较（带检查机会与证据的范围内）")
    mc = comp["method_comparison"]
    lines.append(f"- 范围声明：{mc['scope']}")
    for key in ("aerial_leads", "ground_leads"):
        row = mc[key]
        lines.append(f"- {key}：上报 {row['reported']}，确认 {row['confirmed_open']}，"
                     f"未见 {row['not_found_open']}，修前已修 {row['repaired_before_verification']}，"
                     f"证据不足 {row['inconclusive']}，待核实 {row['pending']}")

    lines.append("")
    lines.append("## 6. 班组容量与待办")
    for audit in payload["capacity_audit"]:
        overload = f"，超名额 {audit['over_by']}" if audit["over_by"] else ""
        lines.append(f"- {audit['crew']} day {audit['day']}：名额 {audit['places']}"
                     f"（含紧急预留 {audit['urgent_reserved']}），"
                     f"计划抽查 {len(audit['planned_draws'])} 个{overload}："
                     f"{', '.join(audit['planned_draws'])}")
    if payload.get("pending_urgent_leads"):
        lines.append("- 仍待核实的紧急线索（值班须派工，延误影响维修时限）：")
        for lead in payload["pending_urgent_leads"]:
            lines.append(f"  - {lead['report']} {lead['segment']} {lead['candidate']}"
                         f"：{lead['status']} / {lead['reason']} {lead['caveat']}")

    lines.append("")
    lines.append("## 7. 可估计性边界")
    for note in payload["estimability_notes"]:
        lines.append(f"- {note}")
    lines.append("")
    lines.append(f"来源文件哈希：`{json.dumps(payload['source_hashes'], ensure_ascii=False)}`")
    lines.append(f"事件日志哈希：`{payload['event_log_hash']}`；"
                 f"快照基于事件序号 {payload['created_after_event_seq']}")
    return "\n".join(lines)


_HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{title}</title>
<style>
body {{ font-family: -apple-system, "Segoe UI", "Noto Sans CJK SC", sans-serif;
  margin: 2rem; color: #1f2933; background: #fafbfc; }}
h1 {{ border-bottom: 2px solid #334e68; padding-bottom: .3rem; }}
h2 {{ color: #243b53; margin-top: 1.8rem; }}
table {{ border-collapse: collapse; margin: .8rem 0; background: #fff; }}
th, td {{ border: 1px solid #bcccdc; padding: .4rem .7rem; text-align: left; font-size: .92rem; }}
th {{ background: #d9e2ec; }}
.note {{ background: #fff3bf; border-left: 4px solid #f0c419; padding: .6rem .9rem; margin: .6rem 0; }}
.muted {{ color: #627d98; }}
.badge {{ display:inline-block; padding:.1rem .5rem; border-radius: 10px;
  background: #d9e2ec; font-size:.8rem; }}
code {{ background: #eef1f4; padding: .05rem .3rem; border-radius: 4px; }}
</style></head><body>
{body}
</body></html>"""


def _table(headers: list[str], rows: list[list]) -> str:
    head = "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)
    body_rows = []
    for row in rows:
        cells = "".join(f"<td>{html.escape(str(c))}</td>" for c in row)
        body_rows.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def render_html(payload: dict) -> str:
    comp = payload["comparison"]
    cohort = payload["cohort"]
    parts = [f"<h1>{html.escape(payload['title'])}</h1>"]
    parts.append(f"<p class='badge'>{html.escape(payload['report_id'])}</p>")

    parts.append("<h2>1. 人群、时期与观察单位</h2>")
    parts.append("<ul>" + "".join(
        f"<li>{html.escape(x)}</li>" for x in (
            cohort["name"] + f"（纳入 {len(cohort['included_segments'])} 个 v1 路段）",
            cohort["period"], cohort["unit"],
        )
    ) + "</ul>")
    parts.append(_table(["排除路段", "原因"],
                        [[i["segment"], i["reason"]] for i in cohort["excluded"]]))

    parts.append("<h2>2. 漏检可估计范围（仅随机抽查窗口）</h2>")
    rows = []
    for w in comp["windows"]:
        rows.append([
            w["label"], w["n_sampled"], w["observed_misses"],
            w["prior_evidence_misses"], w["population_size"],
            w["covered_segments"], w["unknown_segments"],
            _num(w["ht_total_misses"], 1), _num(w["ht_se"], 1),
            _pct(w["estimated_rate"]),
            f"[{_pct(w['rate_ci95'][0])}, {_pct(w['rate_ci95'][1])}]",
            ", ".join(w["draw_ids"]),
        ])
    parts.append(_table(
        ["窗口", "抽中", "open漏报", "事前证据repaired", "clear总体",
         "覆盖", "窗口外未知", "HT总数", "SE", "漏检率", "95%区间", "抽查"],
        rows,
    ))

    parts.append("<h2>3. 误报与确认（描述性）</h2>")
    rows = []
    for key, w in comp["descriptive"].items():
        rows.append([
            w["label"], w["n"], w["confirmed"], w["not_found"],
            w["repaired_before"], w["inconclusive"], w["pending"],
            _pct(w["not_found_rate"]),
            f"[{_pct(w['not_found_ci95'][0])}, {_pct(w['not_found_ci95'][1])}]",
        ])
    parts.append(_table(
        ["口径", "线索", "确认", "未见", "修前已修", "不足", "待核实", "未见率", "Wilson95%"],
        rows,
    ))

    parts.append("<h2>4. 现场漏报证据</h2>")
    parts.append(_table(
        ["结果", "路段", "方式", "抽查类型", "缺陷", "偏差"],
        [[m["result"], m["segment"], m["method"], m["draw_type"], m["defect"],
          "是" if m["deviation"] else ""] for m in comp["observed_miss_outcomes"]],
    ))

    parts.append("<h2>5. 班组容量</h2>")
    parts.append(_table(
        ["班组", "日", "总名额", "紧急预留", "计划数", "超额", "计划抽查"],
        [[a["crew"], a["day"], a["places"], a["urgent_reserved"],
          len(a["planned_draws"]), a["over_by"], ", ".join(a["planned_draws"])]
         for a in payload["capacity_audit"]],
    ))

    parts.append("<h2>6. 待办紧急线索</h2>")
    if payload["pending_urgent_leads"]:
        parts.append(_table(
            ["线索", "路段", "缺陷", "状态", "去向", "说明"],
            [[l["report"], l["segment"], l["candidate"], l["status"], l["reason"], l["caveat"]]
             for l in payload["pending_urgent_leads"]],
        ))
    else:
        parts.append("<p class='muted'>无待核实紧急线索。</p>")

    parts.append("<h2>7. 可估计性边界</h2>")
    parts.append("".join(f"<div class='note'>{html.escape(n)}</div>"
                         for n in payload["estimability_notes"]))
    parts.append(
        f"<p class='muted'>事件日志哈希 <code>{html.escape(str(payload['event_log_hash']))}</code>"
        f"；快照事件序号 {payload['created_after_event_seq']}</p>"
    )
    return _HTML_TEMPLATE.format(title=html.escape(payload["title"]), body="".join(parts))


def write_report_files(payload: dict, out_dir: str | Path, stem: str) -> dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": out_dir / f"{stem}.json",
        "md": out_dir / f"{stem}.md",
        "html": out_dir / f"{stem}.html",
    }
    paths["json"].write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    paths["md"].write_text(render_markdown(payload), encoding="utf-8")
    paths["html"].write_text(render_html(payload), encoding="utf-8")
    return paths
