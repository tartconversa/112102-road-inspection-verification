"""可估计范围与指标计算。

只有独立随机抽查（selection_type=random）携带设计纳入概率，
漏检（未报缺陷路段中实际存在缺陷的比例）只能在“随机窗口”内加权估计；
directed/substituted 抽查只用于描述性误报/确认率，不外推总体。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .domain import (
    AERIAL,
    GROUND,
    DrawEvidence,
    Lead,
    build_round_one_cohort,
    documented_misses,
)
from .model import Dataset, LeadStatus, PhysicalState


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    phat = successes / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


@dataclass
class HTWindow:
    label: str
    include_deviated: bool
    draw_ids: list[str]
    n_sampled: int
    observed_misses: int
    prior_evidence_misses: int       # repaired+事前证据，不计入 open 窗口
    population_size: int            # 目标总体路段数
    covered_segments: int
    unknown_segments: int
    ht_total_misses: float | None
    ht_variance: float | None
    ht_se: float | None
    estimated_rate: float | None
    rate_ci95: tuple[float, float]
    method_breakdown: dict[str, dict] = field(default_factory=dict)


@dataclass
class LeadWindow:
    label: str
    draws: list[str]
    n: int
    confirmed: int
    not_found: int
    repaired_before: int
    inconclusive: int
    pending: int
    not_found_rate: float | None
    not_found_ci95: tuple[float, float]


@dataclass
class ComparisonReport:
    cohort_summary: dict
    windows: list[HTWindow]
    descriptive: dict[str, LeadWindow]
    observed_miss_outcomes: list[dict]
    method_comparison: dict
    estimability_notes: list[str]


def _random_clear_window(
    ds: Dataset,
    evidence: dict[str, DrawEvidence],
    include_deviated: bool,
) -> HTWindow:
    clear_segments = {s.segment for s in ds.surveys if not s.candidate}
    rows = []
    for draw_id, ev in evidence.items():
        if ev.draw.selection_type != "random":
            continue
        if ev.draw.segment not in clear_segments:
            continue
        if not include_deviated and ev.deviation:
            continue
        open_outcomes = [o for o in ev.outcomes if o.physical_state == PhysicalState.OPEN]
        if not open_outcomes:
            continue
        rows.append((ev, open_outcomes))

    n_sampled = len(rows)
    observed = sum(1 for ev, outs in rows if any(o.defect for o in outs))
    prior_evidence = sum(1 for evidence_row in evidence.values()
                         if evidence_row.draw.selection_type == "random"
                         and evidence_row.prior_evidence_defect
                         and evidence_row.draw.segment in clear_segments)

    # HT 加权：权重 1/pi，对“是否观察到缺陷”指示量加权
    ht_total = ht_var = None
    rate = se = None
    ci = (float("nan"), float("nan"))
    if rows:
        weights = []
        indicators = []
        for ev, outs in rows:
            if ev.draw.probability is None:
                continue
            weights.append(1.0 / ev.draw.probability)
            indicators.append(1.0 if any(o.defect for o in outs) else 0.0)
        ht_total = sum(w * y for w, y in zip(weights, indicators))
        # 简化 HT 方差（等概率抽样的逐元素估计；对 0/1 指示量 y=y^2）
        pis = [ev.draw.probability for ev, _ in rows]  # type: ignore[union-attr]
        ht_var = sum(
            ((1 - pi) / (pi * pi)) * (y * y)
            for pi, y in zip(pis, indicators)
        )
        se = math.sqrt(max(0.0, ht_var))
        population = len(clear_segments)
        rate = ht_total / population
        rate_se = se / population
        ci = (max(0.0, rate - 1.96 * rate_se), min(1.0, rate + 1.96 * rate_se))

    breakdown = _method_breakdown(ds, rows)
    return HTWindow(
        label=("随机窗口（含协议偏差路段）" if include_deviated
               else "随机窗口（剔除改派/关闭等偏差路段，敏感性口径）"),
        include_deviated=include_deviated,
        draw_ids=[ev.draw.draw for ev, _ in rows],
        n_sampled=n_sampled,
        observed_misses=observed,
        prior_evidence_misses=prior_evidence,
        population_size=len(clear_segments),
        covered_segments=len({ev.draw.segment for ev, _ in rows}),
        unknown_segments=len(clear_segments) - len({ev.draw.segment for ev, _ in rows}),
        ht_total_misses=ht_total,
        ht_variance=ht_var,
        ht_se=se,
        estimated_rate=rate,
        rate_ci95=ci,
        method_breakdown=breakdown,
    )


def _method_breakdown(ds: Dataset, rows: list[tuple[DrawEvidence, list]]) -> dict:
    out: dict[str, dict] = {}
    for method in (AERIAL, GROUND):
        sub = [(ev, outs) for ev, outs in rows
               if ds.segments[ev.draw.segment].method == method]
        n = len(sub)
        misses = sum(1 for ev, outs in sub if any(o.defect for o in outs))
        ht = None
        if sub:
            ht = sum(
                (1.0 / ev.draw.probability) * (1.0 if any(o.defect for o in outs) else 0.0)
                for ev, outs in sub if ev.draw.probability is not None
            )
        method_pop = sum(
            1 for s in ds.surveys
            if not s.candidate and ds.segments[s.segment].method == method
        )
        out[method] = {
            "sampled": n,
            "observed_misses": misses,
            "weighted_total_misses": ht,
            "clear_population": method_pop,
            "wilson_observed_rate": wilson_interval(misses, n),
        }
    return out


def _lead_window(
    label: str,
    leads: list[Lead],
    method: str | None = None,
    severity: str | None = None,
) -> LeadWindow:
    selected = [
        lead for lead in leads
        if (method is None or lead.method == method)
        and (severity is None or lead.severity == severity)
    ]
    confirmed = sum(lead.status == LeadStatus.CONFIRMED_OPEN for lead in selected)
    not_found = sum(lead.status == LeadStatus.NOT_FOUND_OPEN for lead in selected)
    repaired = sum(lead.status == LeadStatus.REPAIRED_BEFORE_VERIFICATION for lead in selected)
    inconclusive = sum(
        lead.status in {LeadStatus.CLOSED_ORDER_INCONCLUSIVE, LeadStatus.BLOCKED_CLOSED}
        for lead in selected
    )
    pending = sum(lead.status == LeadStatus.PENDING_VERIFICATION for lead in selected)
    resolved = confirmed + not_found
    rate = not_found / resolved if resolved else None
    ci = wilson_interval(not_found, resolved) if resolved else (float("nan"), float("nan"))
    return LeadWindow(
        label=label,
        draws=[lead.report for lead in selected],
        n=len(selected),
        confirmed=confirmed,
        not_found=not_found,
        repaired_before=repaired,
        inconclusive=inconclusive,
        pending=pending,
        not_found_rate=rate,
        not_found_ci95=ci,
    )


def build_comparison(
    ds: Dataset,
    evidence: dict[str, DrawEvidence],
    leads: list[Lead],
) -> ComparisonReport:
    cohort = build_round_one_cohort(ds)
    windows = [
        _random_clear_window(ds, evidence, include_deviated=True),
        _random_clear_window(ds, evidence, include_deviated=False),
    ]
    descriptive = {
        "all": _lead_window("全部已上报线索（描述性，仅限被复核子集）", leads),
        AERIAL: _lead_window("影像辅助 aerial 线索", leads, method=AERIAL),
        GROUND: _lead_window("常规 ground 线索", leads, method=GROUND),
        "urgent": _lead_window("紧急 urgent 线索", leads, severity="urgent"),
        "routine": _lead_window("常规 routine 线索", leads, severity="routine"),
    }
    miss_outcomes = [
        {
            "result": o.result, "segment": o.segment, "draw": o.draw,
            "day": o.day, "defect": o.defect,
            "method": ds.segments[o.segment].method,
            "draw_type": evidence[o.draw].draw.selection_type,
            "deviation": evidence[o.draw].deviation,
        }
        for o in documented_misses(ds, evidence)
    ]
    method_comparison = {
        "scope": "仅在同时具备检查机会与证据的路段内比较；无抽查覆盖的路段不进入结论",
        "aerial_leads": _method_lead_counts(leads, AERIAL),
        "ground_leads": _method_lead_counts(leads, GROUND),
    }
    notes = [
        "漏检只能在随机抽查窗口内估计：未报缺陷且被 random 抽中、并有 open 观察的路段。",
        "repaired 观察是修后路面，配合已完工资料只登记为事前缺陷，不计入 open 阴性/阳性窗口。",
        "directed 派工与 substituted 替代抽查没有设计纳入概率，其误报/确认率只能描述被复核子集。",
        "改派后的替代抽查不继承原抽样概率；主动加查不得在事后当作随机发现。",
        "拆分新段（D6-A/D6-B）是新观察单位，第1轮结论不覆盖，纳入下一轮计划。",
        "未抽中的未报缺陷路段缺陷状态未知，区间不覆盖这部分不确定性，只能通过下一轮预留名额收窄。",
    ]
    return ComparisonReport(
        cohort_summary={
            "name": cohort.name,
            "period": cohort.period_label,
            "unit": cohort.unit,
            "included_segments": list(cohort.segments),
            "excluded": list(cohort.excluded),
        },
        windows=windows,
        descriptive=descriptive,
        observed_miss_outcomes=miss_outcomes,
        method_comparison=method_comparison,
        estimability_notes=notes,
    )


def _method_lead_counts(leads: list[Lead], method: str) -> dict:
    selected = [lead for lead in leads if lead.method == method]
    return {
        "reported": len(selected),
        "confirmed_open": sum(l.status == LeadStatus.CONFIRMED_OPEN for l in selected),
        "not_found_open": sum(l.status == LeadStatus.NOT_FOUND_OPEN for l in selected),
        "repaired_before_verification": sum(
            l.status == LeadStatus.REPAIRED_BEFORE_VERIFICATION for l in selected
        ),
        "pending": sum(l.status == LeadStatus.PENDING_VERIFICATION for l in selected),
        "inconclusive": sum(
            l.status in {LeadStatus.CLOSED_ORDER_INCONCLUSIVE, LeadStatus.BLOCKED_CLOSED}
            for l in selected
        ),
    }
