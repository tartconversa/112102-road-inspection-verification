"""值班工作流：派工确认、名额改派、关闭重派、下一轮计划与证据更正。

关键约束：
- 原试点名单与 sources/ 台账永不修改；所有变化以只追加事件落盘。
- 紧急线索必须可派工；容量含 urgent_reserved 预留。
- 调走已入独立抽查名额时必须登记替代对象，替代抽查不继承抽样概率，
  且标记 active_addition，不能在事后当作随机发现。
- 同一路段/缺陷的第二次复核若并入既有缺陷组，只计一份现场成果。
- 临时关闭路段同时落在两个计划中时，输出可执行改派，不静默改名单。
- 下一轮独立抽查必须为未报缺陷（clear）路段保留名额。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .domain import (
    AERIAL,
    GROUND,
    UnexecutedReason,
    build_defect_groups,
    classify_leads,
    split_key,
)
from .model import (
    Capacity,
    Dataset,
    Draw,
    LeadStatus,
    PhysicalState,
    SelectionType,
    Work,
    WorkState,
)
from .store import Store, StoreError

CLEAR = "clear"


@dataclass
class RuntimeState:
    ds: Dataset
    rounds: list[dict] = field(default_factory=list)
    capacities: dict[tuple[str, int], dict] = field(default_factory=dict)
    closures: dict[str, dict] = field(default_factory=dict)          # segment -> 事件
    runtime_draws: dict[str, dict] = field(default_factory=dict)     # draw id -> 运行期抽查
    base_draw_override: dict[str, dict] = field(default_factory=dict)  # 对基础台账抽查的状态标记
    arrivals: list[dict] = field(default_factory=list)
    repairs: list[dict] = field(default_factory=list)
    corrections: list[dict] = field(default_factory=list)
    plans: list[dict] = field(default_factory=list)
    lineage: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    plan_counters: dict[int, int] = field(default_factory=dict)

    def current_round(self) -> int:
        return self.rounds[-1]["round"] if self.rounds else 1

    def capacity_for(self, crew: str, day: int) -> dict | None:
        return self.capacities.get((crew, day))

    def used_places(self, crew: str, day: int, releasing_draw: str | None = None) -> int:
        return sum(
            1 for draw in self._all_draw_records()
            if draw.get("crew") == crew and draw.get("planned_day") == day
            and draw.get("status") not in {"cancelled", "rerouted"}
            and draw.get("draw") != releasing_draw
        )

    def urgent_used(self, crew: str, day: int) -> int:
        return sum(
            1 for draw in self._all_draw_records()
            if draw.get("crew") == crew and draw.get("planned_day") == day
            and draw.get("purpose") == "urgent"
            and draw.get("status") not in {"cancelled", "rerouted"}
        )

    def _all_draw_records(self) -> list[dict]:
        base = [
            {
                "draw": d.draw, "segment": d.segment, "planned_day": d.planned_day,
                "selection_type": d.selection_type, "probability": d.probability,
                "crew": d.crew, "status": "planned", "purpose": "urgent" if self._is_urgent(d.segment) else "routine",
                "origin": "pilot",
            }
            for d in self.ds.draws
        ]
        base_overrides = {d["draw"]: d for d in self.base_draw_override.values()}
        for record in base:
            record.update(base_overrides.get(record["draw"], {}))
        return base + list(self.runtime_draws.values())

    def _is_urgent(self, segment: str) -> bool:
        return any(
            s.segment == segment and s.severity == "urgent" and s.candidate
            for s in self.ds.surveys
        )

    def lead_index(self) -> dict[str, dict]:
        groups = build_defect_groups(self.ds)
        leads = classify_leads(self.ds, groups)
        index = {
            lead.report: {
                "report": lead.report, "segment": lead.segment,
                "candidate": lead.candidate, "severity": lead.severity,
                "status": lead.status.value, "reason": lead.reason.value,
                "method": lead.method, "checked": lead.checked,
                "caveat": lead.caveat,
                "evidence": [
                    {"kind": e.kind, "ref": e.ref, "detail": e.detail}
                    for e in lead.evidence
                ],
            }
            for lead in leads
        }
        for event in self.events:
            if event["type"] == "evidence_correction":
                payload = event["payload"]
                index[payload["report"]] = {
                    **index.get(payload["report"], {}),
                    "status": payload["new_status"],
                    "reason": payload.get("new_reason", index.get(payload["report"], {}).get("reason")),
                    "caveat": payload["note"],
                    "corrected_by": event["id"],
                }
            if event["type"] == "merge_confirmation":
                payload = event["payload"]
                for report in payload.get("merged_reports", []):
                    if report in index:
                        index[report] = {
                            **index[report],
                            "status": LeadStatus.MERGED_INTO_GROUP.value,
                            "reason": UnexecutedReason.MERGED.value,
                            "caveat": f"经 {event['id']} 并入缺陷组 {payload['group_label']}，"
                                      "本路面仍未单独观察，保留不确定性，不再单独派工",
                            "merged_into": payload.get("kept_report"),
                        }
        return index


class Workflow:
    def __init__(self, ds: Dataset, store: Store):
        self.ds = ds
        self.store = store
        self.state = self._replay()

    # ---------- 重放 ----------
    def _replay(self) -> RuntimeState:
        state = RuntimeState(ds=self.ds)
        for event in self.store.load_events():
            state.events.append(event)
            self._apply(state, event, persist=False)
        return state

    def _apply(self, state: RuntimeState, event: dict, persist: bool) -> None:
        etype, payload, day, eid = event["type"], event["payload"], event["day"], event["id"]
        if etype == "round_start":
            state.rounds.append({"round": payload["round"], "start_day": day, "note": payload.get("note", "")})
        elif etype == "capacity_set":
            state.capacities[(payload["crew"], day)] = {
                "crew": payload["crew"], "day": day,
                "places": payload["places"], "urgent_reserved": payload["urgent_reserved"],
            }
        elif etype == "segment_closed":
            state.closures[payload["segment"]] = {
                "segment": payload["segment"], "day": day,
                "reason": payload["reason"], "event": eid,
            }
        elif etype == "segment_reopened":
            state.closures.pop(payload["segment"], None)
        elif etype == "urgent_dispatch":
            self._check_capacity(state, payload["crew"], day, urgent=True)
            state.runtime_draws[payload["draw_id"]] = {
                "draw": payload["draw_id"], "segment": payload["segment"],
                "planned_day": day, "selection_type": SelectionType.DIRECTED.value,
                "probability": None, "crew": payload["crew"],
                "status": "planned", "purpose": "urgent",
                "origin": "dispatch", "linked_report": payload.get("report"),
                "active_addition": True,
            }
        elif etype == "substitution":
            original = self._find_draw(state, payload["draw_id"])
            rerouted = {
                **original,
                "status": "rerouted",
                "rerouted_to": payload["substitute_draw_id"],
                "reroute_event": eid,
            }
            if payload["draw_id"] in state.runtime_draws:
                state.runtime_draws[payload["draw_id"]] = rerouted
            else:
                state.base_draw_override[payload["draw_id"]] = rerouted
            state.runtime_draws[payload["substitute_draw_id"]] = {
                "draw": payload["substitute_draw_id"],
                "segment": payload["to_segment"], "planned_day": day,
                "selection_type": SelectionType.SUBSTITUTED.value,
                "probability": None, "crew": payload["crew"],
                "status": "planned", "purpose": original.get("purpose", "routine"),
                "origin": "substitution", "replaces_draw": payload["draw_id"],
                "active_addition": True,
            }
        elif etype == "arrival":
            self._record_arrival(state, eid, day, payload)
        elif etype == "repair_completed":
            state.repairs.append({
                "job": payload["job"], "segment": payload["segment"],
                "defect": payload["defect"], "day": day,
                "state": WorkState.REPAIRED.value, "event": eid,
            })
        elif etype == "evidence_correction":
            state.corrections.append({"event": eid, "day": day, **payload})
        elif etype == "merge_confirmation":
            state.lineage.append({"event": eid, "day": day, **payload})
        elif etype == "plan_finalized":
            state.plans.append({"event": eid, "day": day, **payload})
            for draw in payload["draws"]:
                state.runtime_draws[draw["draw"]] = {
                    **draw, "planned_day": day, "status": "planned",
                }
        else:
            raise StoreError(f"未知事件类型：{etype}")
        if persist:
            self.store.append_event(etype, day, payload, eid)

    # ---------- 对外命令 ----------
    def start_round(self, round_no: int, day: int, note: str = "",
                    event_id: str = "") -> dict:
        if any(r["round"] == round_no for r in self.state.rounds):
            raise StoreError(f"第 {round_no} 轮已开始")
        return self._emit("round_start", day, {"round": round_no, "note": note}, event_id)

    def set_capacity(self, crew: str, day: int, places: int, urgent_reserved: int,
                     event_id: str) -> dict:
        if urgent_reserved > places:
            raise StoreError("urgent_reserved 不能超过 places 总名额")
        return self._emit("capacity_set", day, {
            "crew": crew, "places": places, "urgent_reserved": urgent_reserved,
        }, event_id)

    def close_segment(self, segment: str, day: int, reason: str, event_id: str,
                      reopen: bool = False) -> dict:
        if reopen:
            if segment not in self.state.closures:
                raise StoreError(f"路段 {segment} 当前未关闭")
            return self._emit("segment_reopened", day, {"segment": segment}, event_id)
        self._require_segment(segment)
        if segment in self.state.closures:
            raise StoreError(f"路段 {segment} 已处于关闭状态")
        return self._emit("segment_closed", day,
                          {"segment": segment, "reason": reason}, event_id)

    def dispatch_urgent(self, report: str, crew: str, day: int, event_id: str) -> dict:
        lead = self.state.lead_index().get(report)
        if lead is None:
            raise StoreError(f"线索 {report} 不存在")
        if lead["severity"] != "urgent":
            raise StoreError(f"线索 {report} 不是紧急线索，不能占用紧急预留名额")
        draw_id = f"R{self.state.current_round()}-U-{report}"
        return self._emit("urgent_dispatch", day, {
            "draw_id": draw_id, "segment": lead["segment"],
            "crew": crew, "report": report,
        }, event_id)

    def substitute(self, *, draw: str, day: int, event_id: str,
                   to_segment: str = "", reroute_closed: bool = False) -> dict:
        """调走一个已入计划的抽查名额，登记替代对象（无抽样概率）。

        reroute_closed=True 时忽略 to_segment，自动选取同区组替代段，
        用于关闭路段同时命中两个计划的可执行改派。
        """
        if reroute_closed:
            return self.reroute_closed_draw(draw, day, event_id)
        if not to_segment:
            raise StoreError("普通改派需要目标路段")
        self._require_segment(to_segment)
        original = self._find_draw(self.state, draw)
        if original.get("status") == "rerouted":
            raise StoreError(f"抽查 {draw} 的名额已调走，不能重复改派")
        crew = original["crew"]
        # 改派是同名额置换：原抽查释放一个位置，容量校验时不计它
        self._check_capacity(self.state, crew, day,
                             urgent=original.get("purpose") == "urgent",
                             releasing_draw=draw)
        substitute_id = f"{draw}-SUB"
        return self._emit("substitution", day, {
            "draw_id": draw, "substitute_draw_id": substitute_id,
            "to_segment": to_segment, "crew": crew,
        }, event_id)

    def reroute_closed_draw(self, draw: str, day: int, event_id: str) -> dict:
        """关闭路段命中两个计划时的可执行改派：原名额保留、给出替代段与比较说明。"""
        original = self._find_draw(self.state, draw)
        segment = original["segment"]
        if segment not in self.state.closures:
            raise StoreError(f"路段 {segment} 未关闭，无需重派")
        candidate = self._replacement_candidate(segment, original)
        if candidate is None:
            raise StoreError("没有可用替代路段：同区组内找不到开放且未重复占用的路段")
        return self.substitute(draw=draw, day=day, event_id=event_id,
                               to_segment=candidate)

    def record_arrival(self, draw: str, day: int, defect: str,
                       physical_state: str, event_id: str, note: str = "") -> dict:
        draw_record = self._find_draw(self.state, draw)
        if physical_state == PhysicalState.REPAIRED:
            prior = [
                w for w in self._all_works()
                if w.segment == draw_record["segment"] and w.day <= day
                and w.state == "repaired"
            ]
            if not prior:
                raise StoreError(
                    "repaired 观察缺少事前完工资料；请先登记 repair_completed 或保留未知"
                )
        payload = {
            "draw_id": draw, "segment": draw_record["segment"], "day": day,
            "defect": defect, "physical_state": physical_state, "note": note,
        }
        return self._emit("arrival", day, payload, event_id)

    def complete_repair(self, job: str, segment: str, defect: str, day: int,
                        event_id: str) -> dict:
        self._require_segment(segment)
        return self._emit("repair_completed", day, {
            "job": job, "segment": segment, "defect": defect,
        }, event_id)

    def correct_evidence(self, report: str, new_status: str, day: int,
                         note: str, event_id: str,
                         new_reason: str | None = None) -> dict:
        if report not in self.state.lead_index():
            raise StoreError(f"线索 {report} 不存在，无法更正")
        LeadStatus(new_status)  # 校验枚举
        return self._emit("evidence_correction", day, {
            "report": report, "new_status": new_status,
            "new_reason": new_reason, "note": note,
        }, event_id)

    def confirm_merge(self, *, group_label: str, merged_reports: list[str],
                      kept_report: str, day: int, event_id: str) -> dict:
        """merged_reports 必须是已上报线索；kept_report 可以是现场结果/抽查等证据编号。"""
        index = self.state.lead_index()
        for report in merged_reports:
            if report not in index:
                raise StoreError(f"待合并线索 {report} 不存在，无法确认合并")
        known_results = {o.result for o in self.ds.outcomes}
        if kept_report not in index and kept_report not in known_results:
            raise StoreError(
                f"保留证据 {kept_report} 既不是线索也不是现场结果，无法确认合并"
            )
        return self._emit("merge_confirmation", day, {
            "group_label": group_label, "merged_reports": merged_reports,
            "kept_report": kept_report,
        }, event_id)

    # ---------- 下一轮计划 ----------
    def plan_next_round(self, round_no: int, day: int, crews: list[str],
                        negative_fraction: float = 0.5,
                        event_id: str = "") -> dict:
        """生成下一轮计划：必须给未报缺陷路段保留名额。"""
        if not 0.0 < negative_fraction <= 1.0:
            raise StoreError("negative_fraction 必须在 (0,1]")
        total_places = 0
        crew_places: dict[str, int] = {}
        for crew in crews:
            cap = self.state.capacity_for(crew, day)
            if cap is None:
                raise StoreError(f"班组 {crew} 在 day {day} 未设置容量")
            total_places += cap["places"]
            crew_places[crew] = cap["places"]
        negative_places = max(1, round(total_places * negative_fraction))

        eligible = self._eligible_segments(round_no)
        # 关闭段不进入随机抽取池（directed 池同样剔除），改派由专门命令处理
        clear_segments = [s for s in eligible
                          if self._survey_status(s) == CLEAR and s not in self.state.closures]
        candidate_segments = [s for s in eligible
                              if self._survey_status(s) != CLEAR and s not in self.state.closures]
        # 确定性抽取：按编号排序后等距选取，保证可复跑
        negative_pick = self._equidistant_pick(clear_segments, negative_places)
        remaining = total_places - len(negative_pick)
        directed_pick = self._equidistant_pick(
            [s for s in candidate_segments if s not in self.state.closures], remaining
        )
        # 按各班组剩余容量依次派班，超容量直接拒绝而不是静默超额
        draws = []
        crew_cursor = {crew: 0 for crew in crews}

        def assign(segment: str, selection_type: SelectionType, purpose: str) -> None:
            for crew in crews:
                if crew_cursor[crew] < crew_places[crew]:
                    crew_cursor[crew] += 1
                    draws.append(self._plan_draw(
                        round_no, segment, crew, day, selection_type, purpose))
                    return
            raise StoreError("派班超出班组容量总和，无法完成计划")

        for segment in negative_pick:
            assign(segment, SelectionType.RANDOM, "negative_slot")
        for segment in directed_pick:
            assign(segment, SelectionType.DIRECTED, "lead_followup")
        closed_hits = [
            draw["draw"] for draw in draws
            if draw["segment"] in self.state.closures
        ]
        plan_payload = {
            "round": round_no, "planned_day": day, "crews": crews,
            "total_places": total_places,
            "negative_fraction": negative_fraction,
            "negative_places_reserved": len(negative_pick),
            "draws": draws,
            "closed_segment_hits": closed_hits,
            "comparability": self._plan_comparability(draws),
        }
        if event_id:
            return self._emit("plan_finalized", day, plan_payload, event_id)
        return plan_payload

    def _plan_comparability(self, draws: list[dict]) -> list[str]:
        notes = [
            "random 计划抽取保留纳入概率，可继续估计漏检；directed 仅描述性。",
            "未被任何计划覆盖的路段缺陷状态仍未知，结论不外推。",
        ]
        if any(d["segment"] in self.state.closures for d in draws):
            notes.append("存在关闭路段命中计划：请用 reroute_closed_draw 生成可执行改派，"
                         "原名单保持不变并记录替代关系。")
        return notes

    def _plan_draw(self, round_no: int, segment: str, crew: str, day: int,
                   selection_type: SelectionType, purpose: str) -> dict:
        # 纳入概率：仅对覆盖总体的随机计划给出，等概率设计；
        # 总体为该轮可进入随机池的 clear（未报缺陷）且未关闭路段
        probability = None
        if selection_type == SelectionType.RANDOM:
            population = sum(
                1 for s in self._eligible_segments(round_no)
                if self._survey_status(s) == CLEAR and s not in self.state.closures
            )
            probability = round(1.0 / population, 4) if population else None
        # 轮内稳定序号，保证重放产生相同编号
        seq = self.state.plan_counters.get(round_no, 0) + 1
        self.state.plan_counters[round_no] = seq
        return {
            "draw": f"R{round_no}-{selection_type.value[0].upper()}-{seq:02d}-{segment}",
            "segment": segment, "crew": crew,
            "selection_type": selection_type.value,
            "probability": probability, "purpose": purpose,
            "status": "planned", "origin": "round_plan",
        }

    # 场景/CLI 命令名别名（replay 通过命令名反射调用）
    dispatch = dispatch_urgent
    arrival = record_arrival
    repair = complete_repair
    merge = confirm_merge
    correct = correct_evidence
    plan = plan_next_round
    close = close_segment
    capacity = set_capacity

    # ---------- 查询 ----------
    def trace(self, key: str) -> dict:
        """线索(report)或抽查(draw)的完整去向：已修复/已合并/名额调整/待核实/关闭。"""
        leads = self.state.lead_index()
        events = [e for e in self.state.events if self._event_mentions(e, key)]
        record: dict = {"key": key, "matched_lead": leads.get(key), "events": []}
        # 若是 draw，反查关联线索
        draw = self._find_draw_or_none(self.state, key)
        if draw is not None:
            record["draw"] = {k: draw.get(k) for k in
                              ("draw", "segment", "selection_type", "probability",
                               "crew", "status", "purpose", "origin", "active_addition",
                               "replaces_draw", "rerouted_to")}
            linked = draw.get("linked_report") or draw.get("replaces_draw")
            if linked and linked in leads:
                record["linked_lead"] = leads[linked]
        # 汇总与该 key 直接或经派工关联的到场记录
        related_draws = {key}
        for event in events:
            if event["type"] == "urgent_dispatch" and event["payload"].get("report") == key:
                related_draws.add(event["payload"]["draw_id"])
            if event["type"] == "substitution" and key in (
                event["payload"].get("draw_id"),
                event["payload"].get("substitute_draw_id"),
            ):
                related_draws.add(event["payload"]["draw_id"])
                related_draws.add(event["payload"]["substitute_draw_id"])
        record["arrivals"] = [
            a for a in self.state.arrivals if a["draw_id"] in related_draws
        ]
        record["events"] = [
            {"seq": e["seq"], "id": e["id"], "type": e["type"], "day": e["day"],
             "payload": e["payload"]}
            for e in events
        ]
        record["classification"] = self._classify_trace(record)
        return record

    def backlog(self) -> dict:
        leads = self.state.lead_index()
        # 运行期已通过到场解决的线索（open/repaired/closed 均算已到场有结论）
        resolved_by_arrival: set[str] = set()
        for event in self.state.events:
            if event["type"] == "urgent_dispatch":
                report = event["payload"].get("report")
                draw_id = event["payload"].get("draw_id")
                if report and any(a["draw_id"] == draw_id for a in self.state.arrivals):
                    resolved_by_arrival.add(report)
        pending, urgent = [], []
        for report, lead in sorted(leads.items()):
            if report in resolved_by_arrival:
                continue
            if lead["status"] == LeadStatus.PENDING_VERIFICATION.value:
                pending.append(lead)
                if lead["severity"] == "urgent":
                    urgent.append(lead)
        return {
            "pending_leads": pending,
            "pending_urgent_leads": urgent,
            "resolved_by_runtime_arrival": sorted(resolved_by_arrival),
            "open_runtime_draws": [
                d for d in self.state.runtime_draws.values() if d["status"] == "planned"
            ],
            "closures": list(self.state.closures.values()),
            "repairs": self.state.repairs,
        }

    def to_combined_dataset(self) -> Dataset:
        """把运行期事件合成为新的只读 Dataset，供下一轮比较复用估计逻辑。"""
        ds = self.ds
        extra_draws, extra_outcomes, extra_works = [], [], []
        for draw in self.state.runtime_draws.values():
            extra_draws.append(Draw(
                draw=draw["draw"], segment=draw["segment"],
                planned_day=draw["planned_day"],
                selection_type=draw["selection_type"],
                probability=draw.get("probability"), crew=draw["crew"],
            ))
        for arrival in self.state.arrivals:
            extra_outcomes.append(
                self._arrival_outcome(arrival)
            )
        for repair in self.state.repairs:
            extra_works.append(Work(
                job=repair["job"], segment=repair["segment"],
                defect=repair["defect"], day=repair["day"], state=repair["state"],
            ))
        # 基础抽查全部保留（含被改派的，其状态在运行期事件中可追踪）；
        # 运行期新增抽查追加在后，观察单位与证据链不丢失。
        kept_base_draws = list(ds.draws)
        return Dataset(
            segments=dict(ds.segments),
            surveys=list(ds.surveys),
            draws=kept_base_draws + extra_draws,
            outcomes=list(ds.outcomes) + extra_outcomes,
            works=list(ds.works) + extra_works,
            dispatch_events=list(ds.dispatch_events),
            capacities=ds.capacities + [
                Capacity(crew=c["crew"], day=c["day"], places=c["places"],
                         urgent_reserved=c["urgent_reserved"])
                for c in self.state.capacities.values()
            ],
            source_hashes=dict(ds.source_hashes),
        )

    # ---------- 内部工具 ----------
    def _emit(self, etype: str, day: int, payload: dict, event_id: str) -> dict:
        if not event_id:
            raise StoreError("事件必须带显式编号，保证可重放")
        record = self.store.append_event(etype, day, payload, event_id)
        self.state.events.append(record)
        self._apply(self.state, record, persist=False)
        return record

    def _check_capacity(self, state: RuntimeState, crew: str, day: int, urgent: bool,
                        releasing_draw: str | None = None) -> None:
        cap = state.capacity_for(crew, day)
        if cap is None:
            raise StoreError(f"班组 {crew} day {day} 未设置容量，无法派工")
        used = state.used_places(crew, day, releasing_draw=releasing_draw)
        if used >= cap["places"]:
            raise StoreError(
                f"班组 {crew} day {day} 名额已满（{used}/{cap['places']}）；"
                "请先做 substitution 或调整容量"
            )
        if (urgent and state.urgent_used(crew, day) >= cap["urgent_reserved"]
                and state.used_places(crew, day, releasing_draw=releasing_draw)
                >= cap["places"]):
            raise StoreError("紧急预留名额与总名额均已满")

    def _require_segment(self, segment: str) -> None:
        if segment not in self.ds.segments:
            raise StoreError(f"未知路段：{segment}")

    def _find_draw(self, state: RuntimeState, draw_id: str) -> dict:
        draw = self._find_draw_or_none(state, draw_id)
        if draw is None:
            raise StoreError(f"未知抽查：{draw_id}")
        return draw

    def _find_draw_or_none(self, state: RuntimeState, draw_id: str) -> dict | None:
        if draw_id in state.runtime_draws:
            return state.runtime_draws[draw_id]
        if draw_id in state.base_draw_override:
            return state.base_draw_override[draw_id]
        for draw in state._all_draw_records():
            if draw["draw"] == draw_id:
                return draw
        return None

    def _record_arrival(self, state: RuntimeState, eid: str, day: int, payload: dict) -> None:
        draw = self._find_draw(state, payload["draw_id"])
        state.arrivals.append({"event": eid, **payload})
        state.runtime_draws.setdefault(draw["draw"], {**draw})
        if draw["draw"] in state.runtime_draws:
            state.runtime_draws[draw["draw"]]["status"] = "completed"
        else:
            state.base_draw_override[draw["draw"]] = {**draw, "status": "completed"}

    def _arrival_outcome(self, arrival: dict):
        from .model import Outcome
        return Outcome(
            result=arrival["event"], draw=arrival["draw_id"],
            segment=arrival["segment"], day=arrival["day"],
            defect=arrival["defect"], physical_state=arrival["physical_state"],
            known_day=arrival["day"],
        )

    def _all_works(self) -> list[Work]:
        return list(self.ds.works) + [
            Work(job=r["job"], segment=r["segment"], defect=r["defect"],
                 day=r["day"], state=r["state"])
            for r in self.state.repairs
        ]

    def _survey_status(self, segment: str) -> str:
        surveys = [s for s in self.ds.surveys if s.segment == segment]
        if not surveys:
            return CLEAR
        return "candidate" if any(s.candidate for s in surveys) else CLEAR

    def _eligible_segments(self, round_no: int) -> list[str]:
        """截至该轮仍有效的观察单位：版本不超过轮次且未被拆分退役。

        D6 v1 在 D6-A/D6-B v2 启用后退役（is_retired），不再出现在新计划里；
        拆分新段从其启用轮起成为独立单位。
        """
        child_parents = {
            seg.prior_segment for seg in self.ds.segments.values() if seg.prior_segment
        }
        segments = []
        for seg_id, seg in self.ds.segments.items():
            if seg.version > round_no:
                continue
            if seg.version < round_no and seg_id in child_parents:
                continue  # 已被拆分退役的旧版本单位
            segments.append(seg_id)
        return sorted(set(segments), key=split_key)

    @staticmethod
    def _equidistant_pick(items: list[str], k: int) -> list[str]:
        if k <= 0 or not items:
            return []
        if k >= len(items):
            return list(items)
        step = len(items) / k
        return [items[int(i * step)] for i in range(k)]

    def _replacement_candidate(self, closed_segment: str, original: dict) -> str | None:
        closed = self.ds.segments[closed_segment]
        child_parents = {
            seg.prior_segment for seg in self.ds.segments.values() if seg.prior_segment
        }
        pool = [
            seg_id for seg_id, seg in self.ds.segments.items()
            if seg.block == closed.block and seg.version == closed.version
            and seg_id != closed_segment
            and seg_id not in self.state.closures
            and seg_id not in child_parents
        ]
        busy = {d["segment"] for d in self.state._all_draw_records()
                if d.get("status") not in {"cancelled", "rerouted"}}
        pool = [s for s in pool if s not in busy]
        if not pool:
            return None
        return sorted(pool, key=split_key)[0]

    def _event_mentions(self, event: dict, key: str) -> bool:
        blob = event["id"] + " " + str(event["payload"])
        return key in blob

    def _classify_trace(self, record: dict) -> dict:
        lead = record.get("matched_lead")
        draw = record.get("draw")
        arrivals = record.get("arrivals", [])
        # 运行期到场/事件优先于基础台账线索状态
        if any(a["physical_state"] == PhysicalState.REPAIRED.value for a in arrivals):
            reason = UnexecutedReason.REPAIRED.value
        elif any(a["physical_state"] == PhysicalState.CLOSED.value for a in arrivals):
            reason = UnexecutedReason.CLOSED.value
        elif draw and draw.get("status") == "rerouted":
            reason = UnexecutedReason.QUOTA_ADJUSTED.value
        elif draw and draw.get("segment") in self.state.closures:
            reason = UnexecutedReason.CLOSED.value
        elif lead and lead.get("reason") in (
            UnexecutedReason.MERGED.value, UnexecutedReason.QUOTA_ADJUSTED.value,
        ):
            reason = lead["reason"]
        elif any(a["physical_state"] == PhysicalState.OPEN.value for a in arrivals):
            reason = UnexecutedReason.NONE.value  # 已到场观察，不再是未执行
        elif lead:
            reason = lead.get("reason")
        else:
            reason = None
        return {
            "unexecuted_reason": reason,
            "is_random_discovery": bool(draw and draw.get("selection_type")
                                        == SelectionType.RANDOM.value
                                        and not draw.get("active_addition")),
            "note": "active_addition 的加查/替代抽查不是随机发现，不携带抽样概率"
                    if draw and draw.get("active_addition") else "",
        }
