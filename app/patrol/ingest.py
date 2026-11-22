"""台账读取与结构校验。

读取时不做任何推断填充：概率留空保持 None，候选缺陷留空保持空串，
路段方式留空（拆分新段）保持空串。不确定性由领域层显式表达。
"""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path

from .model import (
    Capacity,
    Dataset,
    DispatchEvent,
    Draw,
    Outcome,
    Segment,
    Survey,
    Work,
)

HEADERS = {
    "trial_assignment": [
        "segment", "segment_version", "block", "method",
        "start_day", "prior_segment", "inclusion_probability",
    ],
    "survey_reports": ["report", "segment", "day", "candidate", "severity"],
    "verification_draws": [
        "draw", "segment", "planned_day", "selection_type", "probability", "crew",
    ],
    "field_outcomes": [
        "result", "draw", "segment", "day", "defect", "physical_state", "known_day",
    ],
    "works": ["job", "segment", "defect", "day", "state"],
    "dispatch_updates": ["event", "draw", "from_crew", "to_segment", "day", "decision"],
    "crew_capacity": ["crew", "day", "places", "urgent_reserved"],
}


class IngestError(ValueError):
    """输入表违反结构或引用约定。"""


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise IngestError(f"缺少来源文件：{path}")
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != HEADERS[path.stem]:
            raise IngestError(
                f"{path.name} 表头不符：{reader.fieldnames} != {HEADERS[path.stem]}"
            )
        rows = list(reader)
    if not rows:
        raise IngestError(f"{path.name} 为空")
    for row in rows:
        if set(row) != set(HEADERS[path.stem]):
            raise IngestError(f"{path.name} 存在多余或缺失字段")
        if None in row.values():
            raise IngestError(f"{path.name} 存在字段数不对齐的行：{row}")
    return rows


def _int(value: str, where: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise IngestError(f"{where} 需要整数，得到 {value!r}") from exc


def _float_or_none(value: str, where: str) -> float | None:
    if value == "":
        return None
    try:
        number = float(value)
    except ValueError as exc:
        raise IngestError(f"{where} 需要数值，得到 {value!r}") from exc
    if not 0.0 < number <= 1.0:
        raise IngestError(f"{where} 概率必须在 (0,1]，得到 {value!r}")
    return number


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_dataset(source_dir: str | Path) -> Dataset:
    """读取七张来源表并校验跨表引用与基本业务约束。"""
    source_dir = Path(source_dir)
    raw: dict[str, list[dict[str, str]]] = {}
    hashes: dict[str, str] = {}
    for name in HEADERS:
        path = source_dir / f"{name}.csv"
        raw[name] = _read_csv(path)
        hashes[name] = file_sha256(path)

    segments: dict[str, Segment] = {}
    for row in raw["trial_assignment"]:
        key = row["segment"]
        if key in segments:
            raise IngestError(f"segment 重复：{key}")
        segments[key] = Segment(
            segment=key,
            version=_int(row["segment_version"], f"trial_assignment.{key}.segment_version"),
            block=row["block"],
            method=row["method"],
            start_day=_int(row["start_day"], f"trial_assignment.{key}.start_day"),
            prior_segment=row["prior_segment"],
            inclusion_probability=_float_or_none(
                row["inclusion_probability"], f"trial_assignment.{key}.inclusion_probability"
            ),
        )

    surveys = [
        Survey(
            report=r["report"], segment=r["segment"],
            day=_int(r["day"], f"survey_reports.{r['report']}.day"),
            candidate=r["candidate"], severity=r["severity"],
        )
        for r in raw["survey_reports"]
    ]
    draws = [
        Draw(
            draw=r["draw"], segment=r["segment"],
            planned_day=_int(r["planned_day"], f"verification_draws.{r['draw']}.planned_day"),
            selection_type=r["selection_type"],
            probability=_float_or_none(
                r["probability"], f"verification_draws.{r['draw']}.probability"
            ),
            crew=r["crew"],
        )
        for r in raw["verification_draws"]
    ]
    outcomes = [
        Outcome(
            result=r["result"], draw=r["draw"], segment=r["segment"],
            day=_int(r["day"], f"field_outcomes.{r['result']}.day"),
            defect=r["defect"], physical_state=r["physical_state"],
            known_day=_int(r["known_day"], f"field_outcomes.{r['result']}.known_day"),
        )
        for r in raw["field_outcomes"]
    ]
    works = [
        Work(
            job=r["job"], segment=r["segment"], defect=r["defect"],
            day=_int(r["day"], f"works.{r['job']}.day"), state=r["state"],
        )
        for r in raw["works"]
    ]
    dispatch_events = [
        DispatchEvent(
            event=r["event"], draw=r["draw"], from_crew=r["from_crew"],
            to_segment=r["to_segment"],
            day=_int(r["day"], f"dispatch_updates.{r['event']}.day"),
            decision=r["decision"],
        )
        for r in raw["dispatch_updates"]
    ]
    capacities = [
        Capacity(
            crew=r["crew"], day=_int(r["day"], f"crew_capacity.{r['crew']}.day"),
            places=_int(r["places"], f"crew_capacity.{r['crew']}.places"),
            urgent_reserved=_int(
                r["urgent_reserved"], f"crew_capacity.{r['crew']}.urgent_reserved"
            ),
        )
        for r in raw["crew_capacity"]
    ]

    dataset = Dataset(
        segments=segments, surveys=surveys, draws=draws, outcomes=outcomes,
        works=works, dispatch_events=dispatch_events, capacities=capacities,
        source_hashes=hashes,
    )
    _validate_references(dataset)
    return dataset


def _validate_references(ds: Dataset) -> None:
    known_segments = set(ds.segments)
    for seg in ds.segments.values():
        if seg.prior_segment and seg.prior_segment not in known_segments:
            raise IngestError(f"prior_segment 悬空：{seg.segment} -> {seg.prior_segment}")
    for survey in ds.surveys:
        if survey.segment not in known_segments:
            raise IngestError(f"survey_reports.{survey.report} 引用未知路段 {survey.segment}")
    known_draws = {d.draw for d in ds.draws}
    for outcome in ds.outcomes:
        if outcome.draw not in known_draws:
            raise IngestError(f"field_outcomes.{outcome.result} 引用未知抽查 {outcome.draw}")
        if outcome.segment not in known_segments:
            raise IngestError(f"field_outcomes.{outcome.result} 引用未知路段 {outcome.segment}")
    for event in ds.dispatch_events:
        if event.draw not in known_draws:
            raise IngestError(f"dispatch_updates.{event.event} 引用未知抽查 {event.draw}")
        if event.to_segment not in known_segments:
            raise IngestError(f"dispatch_updates.{event.event} 引用未知路段 {event.to_segment}")
    for work in ds.works:
        if work.segment not in known_segments:
            raise IngestError(f"works.{work.job} 引用未知路段 {work.segment}")

    # 同一缺陷编号若出现在多张来源，必须落在同一路段或同一区组的相邻路段。
    _validate_defect_locations(ds)
    # 随机抽查必须带概率，非随机不得带概率。
    for draw in ds.draws:
        if draw.selection_type == "random" and draw.probability is None:
            raise IngestError(f"随机抽查 {draw.draw} 缺少纳入概率")
        if draw.selection_type != "random" and draw.probability is not None:
            raise IngestError(f"非随机抽查 {draw.draw} 不应携带设计概率")
    # repaired 观察必须有修复前的事前证据或已完工资料。
    for outcome in ds.outcomes:
        if outcome.physical_state == "repaired":
            prior = [
                w for w in ds.works
                if w.segment == outcome.segment
                and w.state == "repaired"
                and w.day <= outcome.day
            ]
            if not prior:
                raise IngestError(
                    f"repaired 观察 {outcome.result} 缺少事前完工资料，不能静默视作阴性"
                )


def _validate_defect_locations(ds: Dataset) -> None:
    seen: dict[str, set[str]] = {}
    for survey in ds.surveys:
        if survey.candidate:
            seen.setdefault(survey.candidate, set()).add(survey.segment)
    for outcome in ds.outcomes:
        if outcome.defect:
            seen.setdefault(outcome.defect, set()).add(outcome.segment)
    for work in ds.works:
        seen.setdefault(work.defect, set()).add(work.segment)
    for defect, seg_set in seen.items():
        for segment in seg_set:
            if segment not in ds.segments:
                raise IngestError(f"缺陷 {defect} 引用未知路段 {segment}")
        blocks = {ds.segments[s].block for s in seg_set}
        if len(seg_set) > 1 and len(blocks) != 1:
            raise IngestError(
                f"缺陷 {defect} 跨区组出现于 {sorted(seg_set)}，相邻重复判定只允许同区组"
            )
