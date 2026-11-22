"""不可变领域记录与枚举。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class SelectionType(StrEnum):
    RANDOM = "random"          # 独立随机抽查，带设计纳入概率
    DIRECTED = "directed"      # 日常线索派工，无纳入概率
    SUBSTITUTED = "substituted"  # 名额改派后的替代抽查，不继承原概率


class PhysicalState(StrEnum):
    OPEN = "open"          # 到场时缺陷仍在
    REPAIRED = "repaired"  # 到场前已修复，不能当作修复前阴性
    CLOSED = "closed"      # 路段临时关闭，未能观察路面


class WorkState(StrEnum):
    REPAIRED = "repaired"
    CLOSED = "closed"      # 工单关闭但未记录修复，证据不确定


class Decision(StrEnum):
    CONFIRMED = "confirmed"      # 班组按原安排到场
    SUBSTITUTION = "substitution"  # 名额被调往别处


class LeadStatus(StrEnum):
    CONFIRMED_OPEN = "confirmed_open"                      # 现场确认缺陷存在
    NOT_FOUND_OPEN = "not_found_open"                      # 到场未见缺陷（误报窗口）
    REPAIRED_BEFORE_VERIFICATION = "repaired_before_verification"  # 复核前已修复
    CLOSED_ORDER_INCONCLUSIVE = "closed_order_inconclusive"  # 工单关闭，证据不足
    PENDING_VERIFICATION = "pending_verification"          # 仍待核实
    BLOCKED_CLOSED = "blocked_closed"                      # 路段关闭，本轮无法观察
    MERGED_INTO_GROUP = "merged_into_group"                # 并入相邻段同缺陷组，不单独派工


class UnexecutedReason(StrEnum):
    """未执行线索的四类去向（另含关闭阻断）。"""

    REPAIRED = "repaired"                # 已修复
    MERGED = "merged"                    # 已并入相邻段同一缺陷组
    QUOTA_ADJUSTED = "quota_adjusted"    # 名额调整（改派）
    PENDING = "pending"                  # 仍待核实
    CLOSED = "closed"                    # 临时关闭阻断
    NONE = "none"                        # 已执行，无未执行原因


@dataclass(frozen=True)
class Segment:
    segment: str
    version: int
    block: str
    method: str            # aerial | ground | ""（拆分后尚未分配方式）
    start_day: int
    prior_segment: str
    inclusion_probability: float | None  # 试点纳入概率，区别于抽查概率


@dataclass(frozen=True)
class Survey:
    report: str
    segment: str
    day: int
    candidate: str         # 空串表示未报缺陷
    severity: str


@dataclass(frozen=True)
class Draw:
    draw: str
    segment: str
    planned_day: int
    selection_type: str
    probability: float | None
    crew: str


@dataclass(frozen=True)
class Outcome:
    result: str
    draw: str
    segment: str
    day: int
    defect: str
    physical_state: str
    known_day: int


@dataclass(frozen=True)
class Work:
    job: str
    segment: str
    defect: str
    day: int
    state: str


@dataclass(frozen=True)
class DispatchEvent:
    event: str
    draw: str
    from_crew: str
    to_segment: str
    day: int
    decision: str


@dataclass(frozen=True)
class Capacity:
    crew: str
    day: int
    places: int
    urgent_reserved: int


@dataclass
class Dataset:
    segments: dict[str, Segment]
    surveys: list[Survey]
    draws: list[Draw]
    outcomes: list[Outcome]
    works: list[Work]
    dispatch_events: list[DispatchEvent]
    capacities: list[Capacity]
    source_hashes: dict[str, str] = field(default_factory=dict)

    def segment_version(self, segment: str) -> int:
        return self.segments[segment].version

    def draws_for_segment(self, segment: str) -> list[Draw]:
        return [d for d in self.draws if d.segment == segment]

    def outcomes_for_draw(self, draw: str) -> list[Outcome]:
        return [o for o in self.outcomes if o.draw == draw]
