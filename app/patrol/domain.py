"""领域判定：人群/时期/观察单位、缺陷组、线索状态、抽查证据分级。

原则：
- 观察单位是 segment_version；拆分出的新段是新单位，不回填旧段历史。
- 只有 random 抽查携带设计概率，可支撑加权估计；directed / substituted
  只产出证据，不外推总体。
- 没有到场且无事前证据的记录一律保持“未知”，不填阴性。
- repaired 观察不是修复前阴性。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .model import (
    Dataset,
    Decision,
    Draw,
    LeadStatus,
    Outcome,
    PhysicalState,
    Segment,
    Survey,
    UnexecutedReason,
    Work,
)

AERIAL = "aerial"
GROUND = "ground"


def split_key(segment: str) -> tuple[str, int, str]:
    """D41 -> ('D', 41, '')；D6-A -> ('D', 6, 'A')，用于相邻判定与排序。"""
    match = re.fullmatch(r"([A-Za-z]+)(\d+)(?:-([A-Za-z]+))?", segment)
    if not match:
        return (segment, 0, "")
    prefix, number, suffix = match.groups()
    return (prefix, int(number), suffix or "")


def are_adjacent(ds: Dataset, left: str, right: str) -> bool:
    """同区组、同版本、编号连续即视为相邻段。"""
    a, b = ds.segments[left], ds.segments[right]
    if a.block != b.block or a.version != b.version:
        return False
    ka, kb = split_key(left), split_key(right)
    if ka[0] != kb[0]:
        return False
    if ka[1] != kb[1]:
        return abs(ka[1] - kb[1]) == 1 and ka[2] == "" and kb[2] == ""
    # D6-A / D6-B 这类同号后缀
    return abs(ord(ka[2]) - ord(kb[2])) == 1


@dataclass(frozen=True)
class DefectGroup:
    defect: str
    segments: tuple[str, ...]      # 同区组相邻段
    block: str
    cross_segment: bool

    @property
    def label(self) -> str:
        return f"{self.defect}@{'+'.join(self.segments)}"


def build_defect_groups(ds: Dataset) -> dict[str, DefectGroup]:
    """汇总 survey/outcome/works 中出现过的每个缺陷编号所在路段。"""
    locations: dict[str, set[str]] = {}
    for survey in ds.surveys:
        if survey.candidate:
            locations.setdefault(survey.candidate, set()).add(survey.segment)
    for outcome in ds.outcomes:
        if outcome.defect:
            locations.setdefault(outcome.defect, set()).add(outcome.segment)
    for work in ds.works:
        locations.setdefault(work.defect, set()).add(work.segment)

    groups: dict[str, DefectGroup] = {}
    for defect, segments in locations.items():
        ordered = tuple(sorted(segments, key=split_key))
        blocks = {ds.segments[s].block for s in ordered}
        if len(ordered) > 1:
            pool = list(ordered)
            for seg in pool:
                for other in pool:
                    if seg != other and not are_adjacent(ds, seg, other):
                        # 同区组但不相邻也允许保留（ingest 已拦截跨区组），
                        # 但标记为非相邻重复，避免错误合并派工。
                        pass
        groups[defect] = DefectGroup(
            defect=defect,
            segments=ordered,
            block=next(iter(blocks)),
            cross_segment=len(ordered) > 1,
        )
    return groups


@dataclass
class Evidence:
    kind: str           # outcome | work | dispatch | adjacent_group
    ref: str
    detail: str


@dataclass
class Lead:
    """一条巡查上报线索（survey_reports 中 candidate 非空的记录）。"""

    report: str
    segment: str
    method: str
    severity: str
    day: int
    candidate: str
    status: LeadStatus
    reason: UnexecutedReason
    checked: bool                      # 本路段本体是否有到场观察
    evidence: list[Evidence] = field(default_factory=list)
    caveat: str = ""

    @property
    def executed(self) -> bool:
        return self.status in {
            LeadStatus.CONFIRMED_OPEN,
            LeadStatus.NOT_FOUND_OPEN,
            LeadStatus.REPAIRED_BEFORE_VERIFICATION,
            LeadStatus.CLOSED_ORDER_INCONCLUSIVE,
        }


def _prior_works(ds: Dataset, segment: str, defect: str | None, day: int) -> list[Work]:
    return [
        w for w in ds.works
        if w.segment == segment and w.day <= day
        and (defect is None or w.defect == defect)
    ]


def _open_outcomes(ds: Dataset, segment: str) -> list[Outcome]:
    draw_ids = {d.draw for d in ds.draws_for_segment(segment)}
    return [
        o for o in ds.outcomes
        if o.draw in draw_ids and o.physical_state == PhysicalState.OPEN
    ]


def classify_leads(ds: Dataset, groups: dict[str, DefectGroup]) -> list[Lead]:
    leads: list[Lead] = []
    for survey in ds.surveys:
        if not survey.candidate:
            continue
        seg = ds.segments[survey.segment]
        lead = Lead(
            report=survey.report,
            segment=survey.segment,
            method=seg.method,
            severity=survey.severity,
            day=survey.day,
            candidate=survey.candidate,
            status=LeadStatus.PENDING_VERIFICATION,
            reason=UnexecutedReason.PENDING,
            checked=False,
        )
        group = groups[survey.candidate]
        local_outcomes = _open_outcomes(ds, survey.segment)
        match = next((o for o in local_outcomes if o.defect == survey.candidate), None)
        not_found = next((o for o in local_outcomes if not o.defect), None)

        # 本路段本体的 repaired / 工单证据
        local_draw_ids = {d.draw for d in ds.draws_for_segment(survey.segment)}
        repaired_outcome = next(
            (o for o in ds.outcomes
             if o.draw in local_draw_ids and o.physical_state == PhysicalState.REPAIRED),
            None,
        )
        prior_repair = next(
            (w for w in _prior_works(ds, survey.segment, survey.candidate, 9999)
             if w.state == "repaired"),
            None,
        )
        closed_work = next(
            (w for w in _prior_works(ds, survey.segment, survey.candidate, 9999)
             if w.state == "closed"),
            None,
        )

        if match is not None:
            lead.status = LeadStatus.CONFIRMED_OPEN
            lead.reason = UnexecutedReason.NONE
            lead.checked = True
            lead.evidence.append(Evidence(
                "outcome", match.result,
                f"{match.day} 日现场确认 {match.defect} 仍在（{match.draw}）",
            ))
        elif repaired_outcome is not None and prior_repair is not None:
            lead.status = LeadStatus.REPAIRED_BEFORE_VERIFICATION
            lead.reason = UnexecutedReason.REPAIRED
            lead.checked = True
            lead.evidence.append(Evidence(
                "work", prior_repair.job,
                f"{prior_repair.day} 日已完工修复 {prior_repair.defect}",
            ))
            lead.evidence.append(Evidence(
                "outcome", repaired_outcome.result,
                f"{repaired_outcome.day} 日到场只见 repaired，不能作为修复前阴性",
            ))
            lead.caveat = "复核看到的是修后路面；修复前是否存在缺陷以事前完工资料为准"
        elif not_found is not None:
            lead.status = LeadStatus.NOT_FOUND_OPEN
            lead.reason = UnexecutedReason.NONE
            lead.checked = True
            lead.evidence.append(Evidence(
                "outcome", not_found.result,
                f"{not_found.day} 日到场路面 open 且未见该候选缺陷（{not_found.draw}）",
            ))
            if closed_work is not None:
                lead.evidence.append(Evidence(
                    "work", closed_work.job,
                    f"存在 {closed_work.day} 日 closed 工单 {closed_work.defect}，未记录修复",
                ))
                lead.caveat = "closed 工单不等于修复，误报结论仅基于 open 路面观察"
        elif closed_work is not None:
            lead.status = LeadStatus.CLOSED_ORDER_INCONCLUSIVE
            lead.reason = UnexecutedReason.NONE
            lead.evidence.append(Evidence(
                "work", closed_work.job,
                f"仅有 {closed_work.day} 日 closed 工单，无现场 open 观察，证据不足",
            ))
            lead.caveat = "不能判真也不能判假"

        # 相邻段同一缺陷组的证据：合并线索，不重复计派工成果
        if group.cross_segment:
            neighbor_confirm = _neighbor_confirmation(ds, group, survey.segment)
            if neighbor_confirm is not None:
                lead.evidence.append(Evidence(
                    "adjacent_group", group.label,
                    f"相邻段 {neighbor_confirm.segment} 的 {neighbor_confirm.result} "
                    f"已确认同组缺陷 {group.defect}，两次复核只计一份现场成果",
                ))
                if not lead.checked:
                    lead.status = LeadStatus.PENDING_VERIFICATION
                    lead.reason = UnexecutedReason.MERGED
                    lead.caveat = (
                        f"本路段本体未到场；已并入缺陷组 {group.label} 的相邻段证据，"
                        "本路面是否同样存在缺陷仍保留不确定性"
                    )
        leads.append(lead)
    return leads


def _neighbor_confirmation(ds: Dataset, group: DefectGroup, self_segment: str) -> Outcome | None:
    for outcome in ds.outcomes:
        if (
            outcome.defect == group.defect
            and outcome.segment in group.segments
            and outcome.segment != self_segment
            and outcome.physical_state == PhysicalState.OPEN
        ):
            return outcome
    return None


@dataclass
class DrawEvidence:
    draw: Draw
    effective_type: str           # random | directed | substituted
    deviation: bool               # 协议偏差（改派/关闭/延期）
    deviation_notes: list[str]
    outcomes: list[Outcome]
    informative: bool             # open 且可作阴性/阳性观察
    observed_defect: bool         # open 且记录到缺陷
    prior_evidence_defect: bool   # repaired + 事前完工资料
    survey_had_candidate: bool


def build_draw_evidence(ds: Dataset) -> dict[str, DrawEvidence]:
    """把抽查、派工事件、现场结果串成每条抽查的证据档案。"""
    result: dict[str, DrawEvidence] = {}
    candidates_by_segment = {
        s.segment: s for s in ds.surveys if s.candidate
    }
    for draw in ds.draws:
        notes: list[str] = []
        deviation = False
        effective = draw.selection_type
        outcomes = [o for o in ds.outcomes if o.draw == draw.draw]
        for event in (e for e in ds.dispatch_events if e.draw == draw.draw):
            if event.decision == Decision.SUBSTITUTION:
                effective = "substituted"
                deviation = True
                notes.append(
                    f"{event.day} 日 {event.from_crew} 的本抽查名额改派至 "
                    f"{event.to_segment}（{event.event}）；替代对象不继承原抽样概率"
                )
            elif event.decision == Decision.CONFIRMED:
                notes.append(f"{event.day} 日 {event.from_crew} 按原安排确认到场（{event.event}）")
        closed_works = [w for w in ds.works if w.segment == draw.segment and w.state == "closed"]
        if closed_works and any(o.day > draw.planned_day for o in outcomes):
            deviation = True
            notes.append(
                f"路段 {draw.segment} 因 closed 工单/临时关闭，观察由计划日 "
                f"{draw.planned_day} 延至 {max(o.day for o in outcomes)}"
            )
        if any(o.physical_state == PhysicalState.CLOSED for o in outcomes):
            deviation = True
            notes.append("存在 closed 现场状态，本轮未能观察路面")

        informative = (
            not deviation
            and any(o.physical_state == PhysicalState.OPEN for o in outcomes)
        )
        observed_defect = any(
            o.physical_state == PhysicalState.OPEN and o.defect for o in outcomes
        )
        prior_evidence = any(
            o.physical_state == PhysicalState.REPAIRED
            and _prior_works(ds, o.segment, o.defect or None, o.day)
            for o in outcomes
        )
        result[draw.draw] = DrawEvidence(
            draw=draw,
            effective_type=effective,
            deviation=deviation,
            deviation_notes=notes,
            outcomes=outcomes,
            informative=informative,
            observed_defect=observed_defect,
            prior_evidence_defect=prior_evidence,
            survey_had_candidate=draw.segment in candidates_by_segment,
        )
    return result


@dataclass
class Cohort:
    """一轮比较的人群、时期与观察单位说明。"""

    name: str
    segments: tuple[str, ...]
    excluded: tuple[tuple[str, str], ...]   # (segment, 原因)
    period_label: str
    unit: str


def build_round_one_cohort(ds: Dataset) -> Cohort:
    included, excluded = [], []
    child_parents = {
        seg.prior_segment for seg in ds.segments.values() if seg.prior_segment
    }
    for seg_id, seg in sorted(ds.segments.items(), key=lambda kv: split_key(kv[0])):
        if seg.version != 1:
            excluded.append((seg_id, f"由 {seg.prior_segment} 拆分出的 v{seg.version} 新单位，"
                                     "本轮未分配巡查方式，无观察；自其启用轮起独立计入"))
        elif not seg.method:
            excluded.append((seg_id, "未分配巡查方式"))
        else:
            included.append(seg_id)
    # 被拆分退役的父段单独标注
    retired = sorted(child_parents, key=split_key)
    days = [s.start_day for s in ds.segments.values() if s.version == 1]
    survey_days = [s.day for s in ds.surveys]
    note = ""
    if retired:
        note = "；退役父段 " + "/".join(retired) + " 的历史观察不回填到拆分新段"
    return Cohort(
        name="第1轮试点（v1 路段）",
        segments=tuple(included),
        excluded=tuple(excluded),
        period_label=f"纳入日 {min(days)}–{max(days)}；巡查/抽查观察日 "
                     f"{min(survey_days)}–{max(o.day for o in ds.outcomes)}；"
                     f"拆分新段自 day 13 起另算下一轮{note}",
        unit="segment_version（路段×版本）；D6 v1 与 D6-A/D6-B v2 是不同观察单位，"
             "跨版本比较须沿 prior_segment 世系对齐，不能直接拼接",
    )


@dataclass
class CapacityAudit:
    crew: str
    day: int
    places: int
    urgent_reserved: int
    planned_draws: list[str]
    over_by: int


def audit_capacity(ds: Dataset) -> list[CapacityAudit]:
    audits: list[CapacityAudit] = []
    for cap in sorted(ds.capacities, key=lambda c: (c.day, c.crew)):
        planned = [
            d.draw for d in ds.draws
            if d.crew == cap.crew and d.planned_day == cap.day
        ]
        audits.append(CapacityAudit(
            crew=cap.crew,
            day=cap.day,
            places=cap.places,
            urgent_reserved=cap.urgent_reserved,
            planned_draws=planned,
            over_by=max(0, len(planned) - cap.places),
        ))
    return audits


def documented_misses(ds: Dataset, evidence: dict[str, DrawEvidence]) -> list[Outcome]:
    """现场 open 发现缺陷、但该路段巡查记录报 clear 的漏报证据。"""
    clear_segments = {s.segment for s in ds.surveys if not s.candidate}
    misses = []
    for draw_id, ev in evidence.items():
        for outcome in ev.outcomes:
            if (
                outcome.physical_state == PhysicalState.OPEN
                and outcome.defect
                and outcome.segment in clear_segments
            ):
                misses.append(outcome)
    return sorted(misses, key=lambda o: o.result)


def pending_urgent_leads(leads: list[Lead]) -> list[Lead]:
    return [
        lead for lead in leads
        if lead.severity == "urgent"
        and lead.status == LeadStatus.PENDING_VERIFICATION
    ]
