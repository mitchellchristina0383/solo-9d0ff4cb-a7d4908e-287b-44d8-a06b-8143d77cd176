"""告警时隙整组求解。

问题抽象（不逐周期展开）：

- 一条流选了起点 ``(first_period, offset)`` 后，在周期
  ``first_period, first_period + r, first_period + 2r, ...``（r 为重复间隔）内的
  ``[offset, offset + busy)`` 占用预留段。
- 两条流只有在其周期序列存在公共周期时才可能同周期共存。周期序列
  ``p1 mod r1`` 与 ``p2 mod r2`` 有公共解，当且仅当
  ``p1 ≡ p2 (mod gcd(r1, r2))``（中国剩余定理），因此两两判断即可，
  与周期总数无关。
- 同一公共周期内，两条流的帧区间 ``[o1, o1+b1)`` 与 ``[o2, o2+b2)``
  必须互不重叠。
- 整帧必须落在告警预留段：``0 <= offset`` 且 ``offset + busy <= reserve``。

求解按剪枝强度（候选起点少的流先选）做回溯；每条流的候选起点按
``(首帧绝对起点, 请求内下标)`` 升序尝试，返回第一个完整解，因此结果是
确定性的“各流尽量早”方案。单条产线最多 6 条流 × 6 个起点，
搜索空间上界 6^6，且只做两两相容判断，不会随周期数增长。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import gcd

from .slot_models import AlarmFlowSpec, SelectedSlot, StartPoint


@dataclass(frozen=True)
class FixedFlow:
    """已生效（其他产线）的流，作为不可移动的占用。"""

    flow_id: str
    busy_ms: int
    repeat_every_periods: int
    first_period: int
    offset_ms: int


@dataclass(frozen=True)
class SolveFailure:
    """无解原因，便于 API 返回可读错误。"""

    reason: str
    flow_id: str | None = None


@dataclass(frozen=True)
class _Candidate:
    index: int
    point: StartPoint
    start_ms: int


def _periods_may_meet(first_a: int, repeat_a: int, first_b: int, repeat_b: int) -> bool:
    """两条周期序列是否存在公共周期（CRT 充要条件）。"""
    return (first_a - first_b) % gcd(repeat_a, repeat_b) == 0


def _overlap(offset_a: int, busy_a: int, offset_b: int, busy_b: int) -> bool:
    return max(offset_a, offset_b) < min(offset_a + busy_a, offset_b + busy_b)


def _compatible_with_fixed(
    candidate: _Candidate,
    busy_ms: int,
    repeat_every_periods: int,
    fixed: FixedFlow,
) -> bool:
    if not _periods_may_meet(
        candidate.point.first_period,
        repeat_every_periods,
        fixed.first_period,
        fixed.repeat_every_periods,
    ):
        return True
    return not _overlap(candidate.point.offset_ms, busy_ms, fixed.offset_ms, fixed.busy_ms)


def _compatible(
    a: _Candidate,
    a_busy: int,
    a_repeat: int,
    b: _Candidate,
    b_busy: int,
    b_repeat: int,
) -> bool:
    if not _periods_may_meet(
        a.point.first_period, a_repeat, b.point.first_period, b_repeat
    ):
        return True
    return not _overlap(a.point.offset_ms, a_busy, b.point.offset_ms, b_busy)


def _usable_candidates(
    flow: AlarmFlowSpec, period_ms: int, reserve_ms: int
) -> list[_Candidate]:
    """整帧能落入预留段的起点，按首帧绝对起点升序。"""
    usable = [
        _Candidate(
            index=i,
            point=p,
            start_ms=p.first_period * period_ms + p.offset_ms,
        )
        for i, p in enumerate(flow.start_points)
        if p.offset_ms + flow.busy_ms <= reserve_ms
    ]
    usable.sort(key=lambda c: (c.start_ms, c.point.offset_ms, c.index))
    return usable


def solve_plan(
    flows: list[AlarmFlowSpec],
    period_ms: int,
    reserve_ms: int,
    fixed_flows: list[FixedFlow] | None = None,
) -> tuple[list[SelectedSlot] | None, SolveFailure | None]:
    """为整组流选起点。

    返回 ``(selected, None)`` 或 ``(None, failure)``。所有流都选中才成功。
    """
    fixed_flows = fixed_flows or []
    candidates_by_flow: list[list[_Candidate]] = []
    for flow in flows:
        candidates = _usable_candidates(flow, period_ms, reserve_ms)
        if not candidates:
            return None, SolveFailure(
                reason=(
                    f"流 {flow.flow_id!r} 的起点均无法整帧落入告警预留段"
                    f"（busy_ms={flow.busy_ms}, reserve_ms={reserve_ms}）"
                ),
                flow_id=flow.flow_id,
            )
        candidates_by_flow.append(candidates)

    order = list(range(len(flows)))
    # 候选少的流先选，剪枝更强；输出仍按请求顺序排列。
    order.sort(key=lambda i: len(candidates_by_flow[i]))

    selected: dict[int, _Candidate] = {}
    # 记录“某条流的所有可用起点都被其他产线挡死”的流，用于区分无解原因。
    blocked_flow: str | None = None

    def backtrack(pos: int) -> bool:
        nonlocal blocked_flow
        if pos == len(order):
            return True
        flow_idx = order[pos]
        flow = flows[flow_idx]
        any_passed_fixed = False
        for candidate in candidates_by_flow[flow_idx]:
            if any(
                not _compatible_with_fixed(candidate, flow.busy_ms, flow.repeat_every_periods, f)
                for f in fixed_flows
            ):
                continue
            any_passed_fixed = True
            if any(
                not _compatible(
                    candidate,
                    flow.busy_ms,
                    flow.repeat_every_periods,
                    other,
                    flows[other_idx].busy_ms,
                    flows[other_idx].repeat_every_periods,
                )
                for other_idx, other in selected.items()
            ):
                continue
            selected[flow_idx] = candidate
            if backtrack(pos + 1):
                return True
            del selected[flow_idx]
        if not any_passed_fixed and blocked_flow is None:
            blocked_flow = flow.flow_id
        return False

    if not backtrack(0):
        # 精确区分无解原因：若去掉其他产线的占用后本组有解，则失败完全由其他产线造成。
        if fixed_flows:
            selected_without_fixed, failure_without_fixed = solve_plan(
                flows, period_ms, reserve_ms, fixed_flows=None
            )
            if selected_without_fixed is not None:
                return None, SolveFailure(
                    reason=(
                        "与其他产线已生效的时隙冲突：本组流在所有可用起点上"
                        "都会在公共周期内与既有帧区间重叠"
                        + (f"（流 {blocked_flow!r} 首当其冲）" if blocked_flow else "")
                        + "，其他产线时隙保持原位"
                    ),
                    flow_id=blocked_flow,
                )
            assert failure_without_fixed is not None
        return None, SolveFailure(
            reason="本组流内部无法同时满足：所有起点组合都会在公共周期内发生帧区间重叠"
        )

    result: list[SelectedSlot] = []
    for i, flow in enumerate(flows):
        candidate = selected[i]
        reserve_start = candidate.point.first_period * period_ms
        result.append(
            SelectedSlot(
                flow_id=flow.flow_id,
                busy_ms=flow.busy_ms,
                repeat_every_periods=flow.repeat_every_periods,
                start_point_index=candidate.index,
                start_point=candidate.point,
                first_start_ms=candidate.start_ms,
                first_end_ms=candidate.start_ms + flow.busy_ms,
                reserve_start_ms=reserve_start,
                reserve_end_ms=reserve_start + reserve_ms,
            )
        )
    return result, None


def flows_conflict(a: FixedFlow, b: FixedFlow) -> bool:
    """两条已排定流是否冲突（供测试/自检使用）。"""
    return _periods_may_meet(
        a.first_period, a.repeat_every_periods, b.first_period, b.repeat_every_periods
    ) and _overlap(a.offset_ms, a.busy_ms, b.offset_ms, b.busy_ms)
