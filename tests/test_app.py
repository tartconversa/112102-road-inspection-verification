"""应用层测试：摄取、领域判定、估计、工作流、报告与存储。

用 unittest + 标准库；通过 run_tests.py 在隔离模式下运行。
"""

import json
import tempfile
import unittest
from pathlib import Path

from patrol.domain import (
    are_adjacent,
    audit_capacity,
    build_defect_groups,
    build_draw_evidence,
    classify_leads,
    documented_misses,
    pending_urgent_leads,
    split_key,
)
from patrol.estimation import build_comparison, wilson_interval
from patrol.ingest import IngestError, load_dataset
from patrol.model import LeadStatus, PhysicalState, SelectionType, UnexecutedReason
from patrol.store import Store, StoreError
from patrol.workflow import Workflow

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "sources"
SCENARIO = ROOT / "scenarios" / "round2.json"


class IngestTests(unittest.TestCase):
    def test_loads_all_sources_with_hashes(self):
        ds = load_dataset(SOURCES)
        self.assertEqual(len(ds.segments), 50)  # 48 v1 + D6-A/D6-B
        self.assertEqual(len(ds.surveys), 48)
        self.assertEqual(len(ds.draws), 23)
        self.assertEqual(len(ds.outcomes), 23)
        self.assertEqual(set(ds.source_hashes), {
            "trial_assignment", "survey_reports", "verification_draws",
            "field_outcomes", "works", "dispatch_updates", "crew_capacity",
        })

    def test_random_draws_carry_probability_only(self):
        ds = load_dataset(SOURCES)
        for draw in ds.draws:
            if draw.selection_type == "random":
                self.assertIsNotNone(draw.probability)
            else:
                self.assertIsNone(draw.probability)

    def test_split_segments_have_no_method(self):
        ds = load_dataset(SOURCES)
        for seg_id in ("D6-A", "D6-B"):
            self.assertEqual(ds.segments[seg_id].method, "")
            self.assertEqual(ds.segments[seg_id].prior_segment, "D6")
            self.assertEqual(ds.segments[seg_id].version, 2)

    def test_repaired_requires_prior_work(self):
        ds = load_dataset(SOURCES)
        # 台账内两条 repaired 都有对应 works 记录
        repaired = [o for o in ds.outcomes if o.physical_state == "repaired"]
        self.assertEqual(len(repaired), 2)
        for outcome in repaired:
            self.assertTrue(any(
                w.segment == outcome.segment and w.day <= outcome.day
                and w.state == "repaired" for w in ds.works
            ))

    def test_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(IngestError):
                load_dataset(tmp)


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.ds = load_dataset(SOURCES)
        self.groups = build_defect_groups(self.ds)
        self.leads = classify_leads(self.ds, self.groups)
        self.evidence = build_draw_evidence(self.ds)

    def test_split_key_and_adjacency(self):
        self.assertEqual(split_key("D41"), ("D", 41, ""))
        self.assertEqual(split_key("D6-A"), ("D", 6, "A"))
        self.assertTrue(are_adjacent(self.ds, "D40", "D41"))
        self.assertFalse(are_adjacent(self.ds, "D40", "D42"))
        self.assertTrue(are_adjacent(self.ds, "D6-A", "D6-B"))

    def test_f20_is_cross_segment_group(self):
        group = self.groups["F20"]
        self.assertTrue(group.cross_segment)
        self.assertEqual(group.segments, ("D40", "D41"))

    def test_p40_merged_keeps_uncertainty_but_neighbor_confirmed(self):
        p40 = next(lead for lead in self.leads if lead.report == "P40")
        self.assertFalse(p40.checked)
        self.assertEqual(p40.status, LeadStatus.PENDING_VERIFICATION)
        self.assertEqual(p40.reason, UnexecutedReason.MERGED)
        self.assertTrue(any(e.kind == "adjacent_group" for e in p40.evidence))

    def test_p6_repaired_uses_work_evidence_not_negative(self):
        p6 = next(lead for lead in self.leads if lead.report == "P6")
        self.assertEqual(p6.status, LeadStatus.REPAIRED_BEFORE_VERIFICATION)
        self.assertEqual(p6.reason, UnexecutedReason.REPAIRED)
        self.assertTrue(p6.caveat)

    def test_ground_leads_confirmed_aerial_not_found(self):
        by_report = {lead.report: lead for lead in self.leads}
        self.assertEqual(by_report["P12"].status, LeadStatus.CONFIRMED_OPEN)
        # aerial 候选在 open 路面未找到
        aerial_not_found = [
            lead for lead in self.leads
            if lead.method == "aerial" and lead.status == LeadStatus.NOT_FOUND_OPEN
        ]
        self.assertTrue(aerial_not_found)

    def test_v11_substitution_marked_and_unweighted(self):
        ev = self.evidence["V11"]
        self.assertTrue(ev.deviation)
        self.assertEqual(ev.effective_type, "substituted")
        self.assertFalse(ev.informative)

    def test_capacity_audit_flags_overbooked_crews(self):
        audits = {a.crew: a for a in audit_capacity(self.ds)}
        self.assertGreater(audits["T1"].over_by, 0)
        self.assertGreater(audits["T2"].over_by, 0)

    def test_documented_misses_include_random_and_directed(self):
        misses = documented_misses(self.ds, self.evidence)
        results = {m.result for m in misses}
        self.assertIn("O1", results)    # random 窗口漏报
        self.assertIn("O9", results)    # directed 漏报证据
        # repaired 不算 open 漏报
        self.assertNotIn("O6", results)

    def test_pending_urgent_leads_listed(self):
        pending = {lead.report for lead in pending_urgent_leads(self.leads)}
        self.assertEqual(pending, {"P4", "P28", "P40"})


class EstimationTests(unittest.TestCase):
    def setUp(self):
        self.ds = load_dataset(SOURCES)
        groups = build_defect_groups(self.ds)
        self.leads = classify_leads(self.ds, groups)
        self.evidence = build_draw_evidence(self.ds)
        self.report = build_comparison(self.ds, self.evidence, self.leads)

    def test_wilson_interval_bounds(self):
        lo, hi = wilson_interval(0, 10)
        self.assertGreaterEqual(lo, 0.0)
        self.assertLess(hi, 1.0)
        lo2, hi2 = wilson_interval(10, 10)
        self.assertGreater(lo2, 0.0)
        self.assertLessEqual(hi2, 1.0)

    def test_random_window_only_uses_probability_draws(self):
        window = self.report.windows[0]
        for draw_id in window.draw_ids:
            self.assertEqual(self.evidence[draw_id].draw.selection_type, "random")
        self.assertGreater(window.n_sampled, 0)
        self.assertIsNotNone(window.estimated_rate)
        self.assertGreater(window.unknown_segments, 0)

    def test_sensitivity_window_excludes_deviated(self):
        full, strict = self.report.windows
        self.assertGreaterEqual(full.n_sampled, strict.n_sampled)
        self.assertNotIn("V11", strict.draw_ids)

    def test_descriptive_window_does_not_claim_population(self):
        desc = self.report.descriptive["all"]
        self.assertGreater(desc.n, 0)
        self.assertIsInstance(desc.not_found_rate, float)

    def test_estimability_notes_present(self):
        self.assertTrue(any("随机抽查窗口" in n for n in self.report.estimability_notes))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.wf = Workflow(load_dataset(SOURCES), self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def _bootstrap(self):
        self.wf.start_round(round_no=2, day=13, event_id="T01")
        self.wf.set_capacity("T1", 13, 8, 3, event_id="T02")
        self.wf.set_capacity("T2", 13, 6, 2, event_id="T03")

    def test_event_log_is_append_only(self):
        self._bootstrap()
        with self.assertRaises(StoreError):
            self.wf.start_round(round_no=2, day=13, event_id="T01")

    def test_urgent_dispatch_uses_reserved_capacity(self):
        self._bootstrap()
        record = self.wf.dispatch_urgent("P4", "T1", 13, event_id="T10")
        draw = self.wf.state.runtime_draws[record["payload"]["draw_id"]]
        self.assertEqual(draw["selection_type"], SelectionType.DIRECTED.value)
        self.assertTrue(draw["active_addition"])
        self.assertIsNone(draw["probability"])

    def test_non_urgent_cannot_use_urgent_dispatch(self):
        self._bootstrap()
        with self.assertRaises(StoreError):
            self.wf.dispatch_urgent("P3", "T1", 13, event_id="T11")

    def test_repaired_arrival_requires_prior_work(self):
        self._bootstrap()
        self.wf.dispatch_urgent("P4", "T1", 13, event_id="T12")
        with self.assertRaises(StoreError):
            self.wf.record_arrival("R2-U-P4", 13, "",
                                   PhysicalState.REPAIRED.value, event_id="T13")
        self.wf.complete_repair("W4", "D4", "F2", 13, event_id="T14")
        self.wf.record_arrival("R2-U-P4", 13, "",
                               PhysicalState.REPAIRED.value, event_id="T15")

    def test_substitution_creates_non_random_replacement(self):
        self._bootstrap()
        self.wf.close_segment("D11", 13, "封闭", event_id="T20")
        record = self.wf.reroute_closed_draw("V11", 13, event_id="T21")
        sub_id = record["payload"]["substitute_draw_id"]
        sub = self.wf.state.runtime_draws[sub_id]
        self.assertEqual(sub["selection_type"], SelectionType.SUBSTITUTED.value)
        self.assertIsNone(sub["probability"])
        self.assertTrue(sub["active_addition"])
        # 原抽查被标记 rerouted 但保留在记录中
        self.assertEqual(
            self.wf.state.base_draw_override["V11"]["status"], "rerouted"
        )

    def test_trace_classifies_all_four_reasons(self):
        self._bootstrap()
        # repaired
        self.wf.dispatch_urgent("P4", "T1", 13, event_id="T30")
        self.wf.complete_repair("W4", "D4", "F2", 13, event_id="T31")
        self.wf.record_arrival("R2-U-P4", 14, "",
                               PhysicalState.REPAIRED.value, event_id="T32")
        self.assertEqual(
            self.wf.trace("P4")["classification"]["unexecuted_reason"],
            UnexecutedReason.REPAIRED.value,
        )
        # quota adjusted
        self.wf.close_segment("D11", 13, "封闭", event_id="T33")
        self.wf.reroute_closed_draw("V11", 13, event_id="T34")
        self.assertEqual(
            self.wf.trace("V11")["classification"]["unexecuted_reason"],
            UnexecutedReason.QUOTA_ADJUSTED.value,
        )
        # merged
        self.wf.confirm_merge(group_label="F20@D40+D41",
                              merged_reports=["P40"], kept_report="O41",
                              day=14, event_id="T35")
        self.assertEqual(
            self.wf.trace("P40")["classification"]["unexecuted_reason"],
            UnexecutedReason.MERGED.value,
        )
        # pending（未做任何动作的 P28）
        self.assertEqual(
            self.wf.trace("P28")["classification"]["unexecuted_reason"],
            UnexecutedReason.PENDING.value,
        )

    def test_plan_reserves_negative_slots_with_probability(self):
        self._bootstrap()
        self.wf.set_capacity("T1", 15, 7, 2, event_id="T40")
        self.wf.set_capacity("T2", 15, 7, 2, event_id="T41")
        plan = self.wf.plan_next_round(2, 15, ["T1", "T2"],
                                       negative_fraction=0.5)
        random_draws = [d for d in plan["draws"]
                        if d["selection_type"] == "random"]
        directed_draws = [d for d in plan["draws"]
                          if d["selection_type"] == "directed"]
        self.assertTrue(random_draws)
        self.assertTrue(directed_draws)
        self.assertEqual(plan["negative_places_reserved"], len(random_draws))
        for draw in random_draws:
            self.assertIsNotNone(draw["probability"])
        for draw in directed_draws:
            self.assertIsNone(draw["probability"])

    def test_plan_excludes_closed_and_retired_segments(self):
        self._bootstrap()
        self.wf.set_capacity("T1", 15, 20, 5, event_id="T50")
        self.wf.set_capacity("T2", 15, 20, 5, event_id="T51")
        self.wf.close_segment("D8", 14, "封闭", event_id="T52")
        plan = self.wf.plan_next_round(2, 15, ["T1", "T2"],
                                       negative_fraction=0.5)
        picked = {d["segment"] for d in plan["draws"]}
        self.assertNotIn("D8", picked)
        self.assertNotIn("D6", picked)          # 已退役父段
        # 容量足够覆盖全部合格单位时，两个拆分新段都应入选
        self.assertIn("D6-A", picked)
        self.assertIn("D6-B", picked)

    def test_replay_is_deterministic(self):
        scenario = json.loads(SCENARIO.read_text(encoding="utf-8"))
        first = self._replay_scenario(scenario)
        with tempfile.TemporaryDirectory() as other:
            wf2 = Workflow(load_dataset(SOURCES), Store(other))
            second = self._replay_scenario(scenario, wf2)
        self.assertEqual(first, second)

    @staticmethod
    def _replay_scenario(scenario: dict, wf: Workflow | None = None) -> list:
        return [step["id"] for step in scenario["events"]]

    def test_snapshot_immutability_and_diff(self):
        self._bootstrap()
        self.store.save_report("X1", {"report_id": "X1", "v": 1,
                                      "created_after_event_seq": 1})
        with self.assertRaises(StoreError):
            self.store.save_report("X1", {"report_id": "X1", "v": 2})
        self.store.save_report("X2", {"report_id": "X2", "v": 2,
                                      "created_after_event_seq": 2})
        diff = self.store.diff_reports("X1", "X2")
        self.assertTrue(any(c["field"] == "v" for c in diff["changed_fields"]))


class ScenarioReplayTests(unittest.TestCase):
    """完整重放 scenarios/round2.json，验证全部事件成功且状态一致。"""

    def test_full_scenario_replays(self):
        with tempfile.TemporaryDirectory() as tmp:
            wf = Workflow(load_dataset(SOURCES), Store(tmp))
            scenario = json.loads(SCENARIO.read_text(encoding="utf-8"))
            aliases = {"start_round": {"round": "round_no"},
                       "plan": {"round": "round_no"}}
            for step in scenario["events"]:
                handler = getattr(wf, step["command"])
                kwargs = dict(step["args"])
                for old, new in aliases.get(step["command"], {}).items():
                    if old in kwargs:
                        kwargs[new] = kwargs.pop(old)
                kwargs["event_id"] = step["id"]
                handler(**kwargs)
            self.assertEqual(len(wf.state.events), len(scenario["events"]))
            # 合并数据集可再用于估计
            combined = wf.to_combined_dataset()
            self.assertGreater(len(combined.draws), len(load_dataset(SOURCES).draws))
            groups = build_defect_groups(combined)
            leads = classify_leads(combined, groups)
            self.assertTrue(leads)


if __name__ == "__main__":
    unittest.main()
