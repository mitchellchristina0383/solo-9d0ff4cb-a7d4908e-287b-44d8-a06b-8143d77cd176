"""离散事件重放引擎。

规则（全部时间为整数毫秒，时间轴从 0 开始，仿真区间 [0, replay_end)）：

1. 时间轴被划分为连续周期，每个周期内：
   - 前段 ``[周期起点, 周期起点 + reserve)`` 为告警时段，只允许发告警帧；
   - 后段为图像时段，只允许发图像帧（reserve=period 时没有图像时段；reserve=0 时没有告警时段）。
2. 出口一次只发一帧；同类帧严格按到达顺序发送（相同到达时刻按请求中的先后）。
3. 整帧必须落在当前本类时段内才会开始发送，即 ``now + busy <= 当前时段终点``；
   队首帧放不进当前时段时，同队后续帧也不能插队，本类时段剩余时间空转，等下一个同类时段。
4. 可指定时刻换一套周期/预留。切换只在周期边界生效：
   - 到点若出口正在发帧，等该帧发完（即走到下一个周期边界）再切换；
   - 到点若出口空闲，等到当前周期结束的边界再切换；
   - 排队帧保留；新周期从实际生效时刻重新对齐（生效时刻即新周期的第 0 毫秒）。
5. 告警从到达开始计时；发完即停止计时（耗时 = end - arrival），未发完则算到重放终点
   （耗时 = replay_end - arrival）；耗时严格大于告警时限即超时。
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import FrameResult, ReplayRequest, ReplayResponse, ReplayStats, SwitchInfo


@dataclass
class _Frame:
    idx: int
    fid: str
    ftype: str  # "alarm" | "image"
    arrival: int
    busy: int
    start: int | None = None
    end: int | None = None
    # 状态标记：未到达 / 已到达排队中 / 发送中 / 已发完
    arrived: bool = False
    transmitting: bool = False
    sent: bool = False


def _segment_at(now: int, anchor: int, period: int, reserve: int) -> tuple[str, int]:
    """返回 ``now`` 所属时段的类别与时段终点（半开区间）。"""
    offset = (now - anchor) % period
    if offset < reserve:
        return "alarm", now + (reserve - offset)
    return "image", now + (period - offset)


def _period_end_at(now: int, anchor: int, period: int) -> int:
    """返回严格大于 ``now`` 的下一个周期边界。

    ``now`` 恰为周期边界时返回下一个边界；调用方据此判断“当前周期还剩多少”。
    """
    offset = (now - anchor) % period
    if offset == 0:
        return now + period
    return now + (period - offset)


def _is_period_boundary(now: int, anchor: int, period: int) -> bool:
    return (now - anchor) % period == 0


def simulate(req: ReplayRequest) -> ReplayResponse:
    """执行一次完整重放，返回逐帧发送记录、切换结果与告警超时标记。"""

    frames = [
        _Frame(
            idx=i,
            fid=f.id if f.id is not None else f"F{i + 1}",
            ftype=f.type,
            arrival=f.arrival_ms,
            busy=f.busy_ms,
        )
        for i, f in enumerate(req.frames)
    ]
    # 到达事件按 (时刻, 输入顺序) 排序，保证“同类按到达顺序发送”
    arrivals = sorted(range(len(frames)), key=lambda i: (frames[i].arrival, frames[i].idx))
    arrivals.reverse()  # 末尾为下一个待处理到达

    period = req.period_ms
    reserve = req.reserve_ms
    anchor = 0  # 当前周期对齐起点（0 或换配实际生效时刻）

    switch_requested = req.switch_at_ms is not None
    switch_time = req.switch_at_ms if switch_requested else -1
    switch_armed = False  # 换配时刻已到，等待下一个周期边界生效
    switched = False
    effective_switch: int | None = None

    queues: dict[str, list[_Frame]] = {"alarm": [], "image": []}
    in_flight: _Frame | None = None
    now = 0
    end_t = req.replay_end_ms
    admitted_through = -1  # 已入队到达事件的时间上界，防止帧在到达前被提前入队

    def apply_switch(t: int) -> None:
        nonlocal period, reserve, anchor, switched, effective_switch, switch_armed
        period = req.new_period_ms  # type: ignore[assignment]
        reserve = req.new_reserve_ms  # type: ignore[assignment]
        anchor = t
        switched = True
        switch_armed = False
        effective_switch = t

    def admit_arrivals(upto: int) -> None:
        """把到达时刻 <= ``upto`` 且尚未入队的帧放入对应类别队列。

        仿真时间单调不减；每次调用都排空所有到达时刻 <= upto 的剩余事件，
        ``admitted_through`` 仅用于在异常回退时保护，正常同刻重复调用也会继续排空。
        """
        nonlocal admitted_through
        if upto < admitted_through:
            return
        while arrivals and frames[arrivals[-1]].arrival <= upto:
            fr = frames[arrivals.pop()]
            fr.arrived = True
            queues[fr.ftype].append(fr)
        admitted_through = upto

    def next_same_class_arrival(seg_class: str) -> int | None:
        """尚未入队的下一个本类帧到达时刻（没有则 None）。"""
        for i in reversed(arrivals):
            if frames[i].ftype == seg_class:
                return frames[i].arrival
        return None

    while now < end_t:
        # 统一入队点：每次进入循环（含发送完成、空转跳转后）都把截至 now 的到达事件收入队列。
        admit_arrivals(now)
        pending_switch = switch_requested and not switched

        # 1) 出口正在发帧：等待发送完成
        if in_flight is not None:
            fin = in_flight.end
            assert fin is not None
            if fin >= end_t:
                # 帧跨过/正好触及重放终点：未发完，仿真结束；
                # 已到点的换配被本帧推迟，终点前没有“发完”边界，保持 not_switched。
                now = end_t
                break
            now = fin
            in_flight.transmitting = False
            in_flight.sent = True
            in_flight = None
            # 换配时刻可能在本帧发送期间到点：发完即周期边界，到点则在此刻生效
            if pending_switch and now >= switch_time:
                apply_switch(now)
            continue

        # 2) 出口空闲：换配时刻到点（只武装，等当前周期结束的边界再生效）
        if pending_switch and not switch_armed and now >= switch_time:
            switch_armed = True

        # 2.5) 全局快速路径：出口空闲，且已无排队帧、无未到达帧。
        #      武装后的等待由步骤 3 的边界跳转处理（O(1)），这里只覆盖“帧已全部处理完”的收尾。
        if not queues["alarm"] and not queues["image"] and not arrivals:
            if pending_switch and not switch_armed:
                if switch_time >= end_t:
                    # 换配时刻在终点之后，本次重放不会切换
                    now = end_t
                    break
                # 跳到换配时刻，避免在无帧期间逐周期空转
                now = switch_time
                continue
            if not pending_switch:
                # 无换配：剩余时间只会空转，直接收敛到终点
                now = end_t
                break
            # pending_switch 且 switch_armed：落到步骤 3，按周期边界处理

        # 3) 已武装的换配只在周期边界生效
        if pending_switch and switch_armed:
            if _is_period_boundary(now, anchor, period):
                # now 本身就是周期边界：新周期从此刻起算
                apply_switch(now)
                continue
            boundary = _period_end_at(now, anchor, period)
            if boundary >= end_t:
                # 生效边界不早于重放终点：本次重放内不会切换
                now = end_t
                break
            # 边界之前维持旧配置；旧周期剩余时间不再安排跨边界发送，直接跳到边界
            now = boundary
            continue

        seg_class, seg_end = _segment_at(now, anchor, period, reserve)
        head = queues[seg_class][0] if queues[seg_class] else None

        # 4) 能整帧放进当前本类时段：立即开始发送
        if head is not None and now + head.busy <= seg_end:
            head.start = now
            head.end = now + head.busy
            head.transmitting = True
            queues[seg_class].pop(0)
            in_flight = head
            now = head.end
            continue

        # 5) 本时段发不出帧，选择下一个决策点：
        #    a) 队首帧放不进：严格 FIFO，同队后续帧不许插队，本时段剩余时间必然空转 -> 段末；
        #    b) 本类队列为空：可在“下一个本类帧到达”时刻提前唤醒（到达即评估能否即发）；
        #    c) 始终保留段末兜底；若换配已到点，本周期结束就要切换，周期终点也是决策点。
        candidates: list[int] = [seg_end]
        if head is None:
            nxt_arr = next_same_class_arrival(seg_class)
            if nxt_arr is not None and nxt_arr < end_t:
                candidates.append(nxt_arr)
        if pending_switch and switch_armed:
            candidates.append(_period_end_at(now, anchor, period))
        nxt = min(candidates)
        # seg_end 严格大于 now（半开时段），故 nxt > now，循环必然推进
        now = min(nxt, end_t)

    # ---- 汇总输出 ----
    results: list[FrameResult] = []
    counts = {"sent": 0, "in_progress": 0, "queued": 0, "not_arrived": 0}
    alarm_total = 0
    alarm_timed_out = 0

    for fr in frames:
        if fr.sent:
            status = "sent"
        elif fr.transmitting and in_flight is fr:
            status = "in_progress"
        elif fr.arrived:
            status = "queued"
        else:
            status = "not_arrived"
        counts[status] += 1

        elapsed: int | None = None
        timed_out: bool | None = None
        if fr.ftype == "alarm" and status != "not_arrived":
            alarm_total += 1
            if status == "sent":
                assert fr.end is not None
                elapsed = fr.end - fr.arrival
            else:
                # in_progress / queued：未发完，算到重放终点
                elapsed = end_t - fr.arrival
            timed_out = elapsed > req.alarm_deadline_ms
            if timed_out:
                alarm_timed_out += 1

        results.append(
            FrameResult(
                id=fr.fid,
                type=fr.ftype,  # type: ignore[arg-type]
                arrival_ms=fr.arrival,
                busy_ms=fr.busy,
                start_ms=fr.start,
                end_ms=fr.end if status == "sent" else None,
                status=status,  # type: ignore[arg-type]
                alarm_elapsed_ms=elapsed,
                timed_out=timed_out,
            )
        )

    if not switch_requested:
        switch_info = SwitchInfo(
            requested_at_ms=None, effective_at_ms=None, switched=False, state="not_requested"
        )
    elif switched:
        switch_info = SwitchInfo(
            requested_at_ms=switch_time,
            effective_at_ms=effective_switch,
            switched=True,
            state="switched",
        )
    else:
        # 未切换：要么换配时刻在终点之后，要么到点后等到的边界不早于终点
        switch_info = SwitchInfo(
            requested_at_ms=switch_time,
            effective_at_ms=None,
            switched=False,
            state="not_switched",
        )

    return ReplayResponse(
        replay_end_ms=end_t,
        switch=switch_info,
        initial_period_ms=req.period_ms,
        initial_reserve_ms=req.reserve_ms,
        effective_period_ms=period,
        effective_reserve_ms=reserve,
        frames=results,
        stats=ReplayStats(
            total=len(frames),
            sent=counts["sent"],
            in_progress=counts["in_progress"],
            queued=counts["queued"],
            not_arrived=counts["not_arrived"],
            alarm_total=alarm_total,
            alarm_timed_out=alarm_timed_out,
        ),
    )
