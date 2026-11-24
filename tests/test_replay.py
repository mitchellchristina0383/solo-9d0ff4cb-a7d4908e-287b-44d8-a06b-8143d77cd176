"""重放引擎与 API 的自测。

重点覆盖：
1) 整帧放不进本类时段（含同类队首阻塞、精确贴合可发）
2) 发送过程中改配（等发完再切换、排队帧保留、新周期从生效时刻起算）
另外覆盖：告警超时判定、未切换、跨终点 in_progress、校验错误、HTTP 端到端。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import ReplayRequest
from app.replay import simulate


def _frame(fid: str, ftype: str, arrival: int, busy: int) -> dict:
    return {"id": fid, "type": ftype, "arrival_ms": arrival, "busy_ms": busy}


def _by_id(resp):
    return {f["id"]: f for f in resp["frames"]}


# ---------------------------------------------------------------- 场景一
def test_frame_does_not_fit_segment_with_head_of_line_blocking():
    """周期 100、告警预留 40：[0,40) 告警、[40,100) 图像。

    - A1=60ms 放不进首个 40ms 告警窗；即使 A2=20ms 能放下也不许插队（同类按到达顺序）。
    - I1=50ms 在 t=40 到达，[40,90) 发送。
    - 第二个告警窗 [100,140) 同样只有 40ms，A1 仍放不下；A2 继续被阻塞。
    - 换个更大预留的配置（见另一用例）A1 才能发出；这里验证它始终 queued，
      且不阻塞异类 I1 的正常发送。
    """
    req = ReplayRequest(
        period_ms=100,
        reserve_ms=40,
        alarm_deadline_ms=1000,
        replay_end_ms=300,
        frames=[
            _frame("A1", "alarm", 0, 60),
            _frame("A2", "alarm", 0, 20),
            _frame("I1", "image", 40, 50),
        ],
    )
    resp = simulate(req).model_dump()

    a1, a2, i1 = _by_id(resp)["A1"], _by_id(resp)["A2"], _by_id(resp)["I1"]

    # 第一告警窗 [0,40)：A1 放不进，A2 被队首阻塞，全部空转
    # 第一图像窗 [40,100)：I1 到达即发 [40,90)
    assert (i1["start_ms"], i1["end_ms"], i1["status"]) == (40, 90, "sent")
    # 所有 40ms 告警窗都放不下 60ms 的 A1；严格 FIFO 下 A2 一并排队
    assert a1["status"] == "queued" and a1["start_ms"] is None
    assert a2["status"] == "queued" and a2["start_ms"] is None
    # 没有发生换配
    assert resp["switch"]["state"] == "not_requested"


def test_head_of_line_blocking_clears_in_next_larger_window():
    """队首帧放不进当前窗时后续帧不插队；换配扩大预留后，队首优先、随后续帧依次发出。"""
    req = ReplayRequest(
        period_ms=100,
        reserve_ms=40,
        alarm_deadline_ms=1000,
        replay_end_ms=400,
        frames=[
            _frame("A1", "alarm", 0, 60),   # 首个 40ms 窗放不下
            _frame("A2", "alarm", 0, 20),   # 被 A1 阻塞
            _frame("I1", "image", 40, 50),  # [40,90)
        ],
        switch_at_ms=100,
        new_period_ms=100,
        new_reserve_ms=80,  # 新告警窗 80ms，A1 可整帧落入
    )
    resp = simulate(req).model_dump()
    fr = _by_id(resp)
    assert resp["switch"]["effective_at_ms"] == 100
    # 新周期从 100 起算：告警窗 [100,180)，A1 [100,160)，随后 A2 [160,180)
    assert (fr["A1"]["start_ms"], fr["A1"]["end_ms"], fr["A1"]["status"]) == (100, 160, "sent")
    assert (fr["A2"]["start_ms"], fr["A2"]["end_ms"], fr["A2"]["status"]) == (160, 180, "sent")
    assert (fr["I1"]["start_ms"], fr["I1"]["end_ms"]) == (40, 90)


def test_frame_too_big_never_fits_remains_queued():
    """占线时长大于任何同类时段的帧永远发不出去，保持 queued 且不阻塞异类。"""
    req = ReplayRequest(
        period_ms=50,
        reserve_ms=20,
        alarm_deadline_ms=1000,
        replay_end_ms=150,
        frames=[
            _frame("A_big", "alarm", 0, 25),  # 告警窗只有 20ms，永远放不下
            _frame("I1", "image", 20, 30),    # 图像窗 30ms，[20,50) 精确贴合
        ],
    )
    resp = simulate(req).model_dump()
    fr = _by_id(resp)
    assert fr["A_big"]["status"] == "queued"
    assert fr["A_big"]["start_ms"] is None
    assert (fr["I1"]["start_ms"], fr["I1"]["end_ms"]) == (20, 50)


# ---------------------------------------------------------------- 场景二
def test_switch_during_transmission_waits_for_frame_completion():
    """150 到点换配时出口正在发 I2（140-200）：必须等 200 发完再切换。"""
    req = ReplayRequest(
        period_ms=100,
        reserve_ms=40,
        alarm_deadline_ms=1000,
        replay_end_ms=400,
        frames=[
            _frame("A1", "alarm", 0, 30),     # [0,30)
            _frame("I1", "image", 0, 60),     # [40,100)
            _frame("I2", "image", 100, 60),   # [140,200)，换配时刻 150 落在发送中
            _frame("A2", "alarm", 150, 40),   # 换配时刻才到达；旧配置下无告警窗机会，排队保留
        ],
        switch_at_ms=150,
        new_period_ms=120,
        new_reserve_ms=60,
    )
    resp = simulate(req).model_dump()

    # 实际切换时刻 = 200（I2 发完），而非 150
    assert resp["switch"]["state"] == "switched"
    assert resp["switch"]["requested_at_ms"] == 150
    assert resp["switch"]["effective_at_ms"] == 200
    assert (resp["effective_period_ms"], resp["effective_reserve_ms"]) == (120, 60)

    fr = _by_id(resp)
    assert (fr["A1"]["start_ms"], fr["A1"]["end_ms"]) == (0, 30)
    assert (fr["I1"]["start_ms"], fr["I1"]["end_ms"]) == (40, 100)
    assert (fr["I2"]["start_ms"], fr["I2"]["end_ms"]) == (140, 200)

    # 排队帧保留：新周期从 200 起算，告警窗 [200,260)，A2 立刻发送 [200,240)
    assert (fr["A2"]["start_ms"], fr["A2"]["end_ms"], fr["A2"]["status"]) == (200, 240, "sent")


def test_switch_when_idle_waits_for_period_boundary_and_realigns():
    """出口空闲时换配也要等当前周期结束的边界；新周期以生效时刻为第 0 毫秒重新对齐。"""
    req = ReplayRequest(
        period_ms=100,
        reserve_ms=40,
        alarm_deadline_ms=1000,
        replay_end_ms=300,
        frames=[_frame("A1", "alarm", 170, 20)],  # 170 时旧周期处于图像窗，告警帧排队
        switch_at_ms=170,
        new_period_ms=80,
        new_reserve_ms=30,
    )
    resp = simulate(req).model_dump()
    # 170 到点时出口空闲，等当前周期（100-200）结束，200 边界生效
    assert resp["switch"]["requested_at_ms"] == 170
    assert resp["switch"]["effective_at_ms"] == 200
    fr = _by_id(resp)["A1"]
    # 新周期 [200,230) 为告警窗，A1 立即发送 [200,220)
    assert (fr["start_ms"], fr["end_ms"]) == (200, 220)


def test_switch_at_exact_period_boundary_takes_effect_immediately():
    """换配时刻恰好是周期边界且出口空闲：当刻立即生效。"""
    req = ReplayRequest(
        period_ms=100,
        reserve_ms=40,
        alarm_deadline_ms=1000,
        replay_end_ms=250,
        frames=[_frame("A1", "alarm", 200, 20)],
        switch_at_ms=200,
        new_period_ms=80,
        new_reserve_ms=30,
    )
    resp = simulate(req).model_dump()
    assert resp["switch"]["effective_at_ms"] == 200
    fr = _by_id(resp)["A1"]
    assert (fr["start_ms"], fr["end_ms"]) == (200, 220)


# ---------------------------------------------------------------- 超时与终点
def test_alarm_timeout_boundary_and_elapsed_calculation():
    """耗时 > 时限才算超时：等于时限不超时；未发完按重放终点计时。"""
    req = ReplayRequest(
        period_ms=100,
        reserve_ms=100,  # 全告警
        alarm_deadline_ms=50,
        replay_end_ms=100,
        frames=[
            _frame("A_eq", "alarm", 0, 50),      # [0,50)，耗时 50 == 时限，不超时
            _frame("A_over", "alarm", 50, 40),   # [50,90) 发完，耗时 40 <= 50，不超时
            _frame("A_wait", "alarm", 90, 10),   # [90,100) 正好发到终点，in_progress，算到 100 -> 10
        ],
    )
    resp = simulate(req).model_dump()
    fr = _by_id(resp)
    assert fr["A_eq"]["timed_out"] is False
    assert fr["A_eq"]["alarm_elapsed_ms"] == 50
    assert fr["A_over"]["status"] == "sent"
    assert (fr["A_over"]["start_ms"], fr["A_over"]["end_ms"]) == (50, 90)
    assert fr["A_over"]["alarm_elapsed_ms"] == 40 and fr["A_over"]["timed_out"] is False
    assert fr["A_wait"]["status"] == "in_progress"
    assert fr["A_wait"]["start_ms"] == 90 and fr["A_wait"]["end_ms"] is None
    assert fr["A_wait"]["alarm_elapsed_ms"] == 10 and fr["A_wait"]["timed_out"] is False
    assert resp["stats"]["alarm_total"] == 3
    assert resp["stats"]["alarm_timed_out"] == 0


def test_alarm_timeout_strictly_greater_than_deadline():
    """再补一组明确超时：耗时 51 > 时限 50。"""
    req = ReplayRequest(
        period_ms=100,
        reserve_ms=100,
        alarm_deadline_ms=50,
        replay_end_ms=100,
        frames=[
            _frame("A1", "alarm", 0, 40),     # [0,40) 耗时 40，不超时
            _frame("A2", "alarm", 10, 51),    # [40,91) 耗时 81，超时
            _frame("A3", "alarm", 95, 10),    # 95 到达，下一帧 A2 发完才轮到；排队算到 100 -> 5
        ],
    )
    resp = simulate(req).model_dump()
    fr = _by_id(resp)
    assert fr["A1"]["timed_out"] is False
    assert (fr["A2"]["start_ms"], fr["A2"]["end_ms"]) == (40, 91)
    assert fr["A2"]["alarm_elapsed_ms"] == 81 and fr["A2"]["timed_out"] is True
    assert fr["A3"]["status"] == "queued"
    assert fr["A3"]["alarm_elapsed_ms"] == 5 and fr["A3"]["timed_out"] is False
    assert resp["stats"]["alarm_timed_out"] == 1


def test_not_arrived_frame_and_not_switched_state():
    """终点后到达的帧标记 not_arrived；换配请求被发送中的帧推迟到终点之后则 not_switched。"""
    req = ReplayRequest(
        period_ms=100,
        reserve_ms=40,
        alarm_deadline_ms=1000,
        replay_end_ms=180,
        frames=[
            _frame("I_busy", "image", 40, 60),    # [40,100) 发完
            _frame("I_long", "image", 140, 60),   # 图像窗 [140,200) 内 [140,200) 发送，跨终点(180)
            _frame("A_late", "alarm", 250, 10),   # 终点后到达
        ],
        switch_at_ms=150,  # I_long 发送中（140-200），须等 200 发完，但终点是 180
        new_period_ms=50,
        new_reserve_ms=20,
    )
    resp = simulate(req).model_dump()
    fr = _by_id(resp)
    assert fr["A_late"]["status"] == "not_arrived"
    assert fr["A_late"]["timed_out"] is None and fr["A_late"]["alarm_elapsed_ms"] is None
    assert fr["I_long"]["status"] == "in_progress"
    assert resp["switch"]["state"] == "not_switched"
    assert resp["switch"]["effective_at_ms"] is None
    # 终点时刻仍生效旧配置
    assert resp["effective_period_ms"] == 100 and resp["effective_reserve_ms"] == 40


def test_arrivals_at_end_are_not_arrived_and_default_ids():
    """到达时刻 == 重放终点不算到达；缺省 id 按 F1、F2 生成。"""
    req = ReplayRequest(
        period_ms=10,
        reserve_ms=5,
        alarm_deadline_ms=100,
        replay_end_ms=20,
        frames=[{"type": "alarm", "arrival_ms": 20, "busy_ms": 5}],
    )
    resp = simulate(req).model_dump()
    assert resp["frames"][0]["id"] == "F1"
    assert resp["frames"][0]["status"] == "not_arrived"


def test_reserve_zero_means_only_images():
    """reserve=0：没有告警时段，告警帧永远排队；图像帧连续占用。"""
    req = ReplayRequest(
        period_ms=50,
        reserve_ms=0,
        alarm_deadline_ms=1000,
        replay_end_ms=120,
        frames=[
            _frame("A1", "alarm", 0, 10),
            _frame("I1", "image", 0, 40),
            _frame("I2", "image", 40, 40),
        ],
    )
    resp = simulate(req).model_dump()
    fr = _by_id(resp)
    assert fr["A1"]["status"] == "queued"
    assert (fr["I1"]["start_ms"], fr["I1"]["end_ms"]) == (0, 40)
    # I2 于 40 到达，但 40ms 已放不下（周期 50 边界），下一周期 [50,90) 发送
    assert (fr["I2"]["start_ms"], fr["I2"]["end_ms"]) == (50, 90)


def test_reserve_equals_period_means_only_alarms():
    """reserve=period：没有图像时段，图像帧永远排队。"""
    req = ReplayRequest(
        period_ms=50,
        reserve_ms=50,
        alarm_deadline_ms=1000,
        replay_end_ms=120,
        frames=[
            _frame("I1", "image", 0, 10),
            _frame("A1", "alarm", 0, 30),
            _frame("A2", "alarm", 30, 30),
        ],
    )
    resp = simulate(req).model_dump()
    fr = _by_id(resp)
    assert fr["I1"]["status"] == "queued"
    assert (fr["A1"]["start_ms"], fr["A1"]["end_ms"]) == (0, 30)
    # A2 于 30 到达，但剩余 20ms 放不下 30ms 帧，下一周期 [50,80) 发送
    assert (fr["A2"]["start_ms"], fr["A2"]["end_ms"]) == (50, 80)


def test_switch_requested_during_transmission_takes_effect_at_frame_end():
    """换配时刻落在帧发送区间内：帧发完的边界立即切换（武装路径）。"""
    req = ReplayRequest(
        period_ms=100,
        reserve_ms=100,  # 全告警，帧结束点即周期边界
        alarm_deadline_ms=1000,
        replay_end_ms=200,
        frames=[_frame("A1", "alarm", 0, 60)],
        switch_at_ms=30,  # A1 [0,60) 发送期间到点
        new_period_ms=50,
        new_reserve_ms=25,
    )
    resp = simulate(req).model_dump()
    assert resp["switch"]["requested_at_ms"] == 30
    assert resp["switch"]["effective_at_ms"] == 60
    assert (resp["effective_period_ms"], resp["effective_reserve_ms"]) == (50, 25)
    fr = _by_id(resp)["A1"]
    assert (fr["start_ms"], fr["end_ms"], fr["status"]) == (0, 60, "sent")


def test_no_overlap_and_single_transmission_invariant():
    """不变量：任意两帧发送区间不重叠；每帧区间长度等于 busy；sent 帧终点 <= replay_end。"""
    req = ReplayRequest(
        period_ms=70,
        reserve_ms=30,
        alarm_deadline_ms=120,
        replay_end_ms=500,
        frames=[
            _frame("A1", "alarm", 0, 25),
            _frame("A2", "alarm", 15, 40),
            _frame("I1", "image", 20, 35),
            _frame("A3", "alarm", 90, 20),
            _frame("I2", "image", 100, 50),
            _frame("I3", "image", 260, 20),
        ],
        switch_at_ms=210,
        new_period_ms=90,
        new_reserve_ms=45,
    )
    resp = simulate(req).model_dump()
    intervals = []
    for f in resp["frames"]:
        if f["status"] == "sent":
            assert f["end_ms"] - f["start_ms"] == f["busy_ms"]
            assert f["end_ms"] <= req.replay_end_ms
            intervals.append((f["start_ms"], f["end_ms"], f["id"]))
        elif f["status"] == "in_progress":
            assert f["end_ms"] is None and f["start_ms"] is not None
    intervals.sort()
    for (s1, e1, id1), (s2, e2, id2) in zip(intervals, intervals[1:]):
        assert e1 <= s2, f"区间重叠: {id1} 与 {id2}"
    # 汇总数与逐帧一致
    assert resp["stats"]["total"] == len(resp["frames"])
    assert resp["stats"]["sent"] + resp["stats"]["in_progress"] + resp["stats"][
        "queued"
    ] + resp["stats"]["not_arrived"] == resp["stats"]["total"]


def test_fast_path_huge_replay_end_does_not_spin_per_period():
    """帧早早发完后，即便终点极大也应快速收敛；未来时刻的换配仍正确生效。"""
    req = ReplayRequest(
        period_ms=10,
        reserve_ms=5,
        alarm_deadline_ms=1000,
        replay_end_ms=10_000_000,
        frames=[_frame("A1", "alarm", 0, 5)],
        switch_at_ms=5_000_000,
        new_period_ms=20,
        new_reserve_ms=8,
    )
    # 若退化为逐周期空转，本测试会明显变慢甚至超时
    resp = simulate(req).model_dump()
    fr = _by_id(resp)["A1"]
    assert (fr["start_ms"], fr["end_ms"], fr["status"]) == (0, 5, "sent")
    # 5,000,000 恰为周期 10 的边界，到点立即生效
    assert resp["switch"]["state"] == "switched"
    assert resp["switch"]["effective_at_ms"] == 5_000_000
    assert resp["effective_period_ms"] == 20


def test_fast_path_switch_after_replay_end_is_not_switched():
    """换配时刻晚于重放终点：标记 not_switched，终点时刻仍为旧配置。"""
    req = ReplayRequest(
        period_ms=10,
        reserve_ms=5,
        alarm_deadline_ms=1000,
        replay_end_ms=100,
        frames=[_frame("A1", "alarm", 0, 5)],
        switch_at_ms=200,
        new_period_ms=20,
        new_reserve_ms=8,
    )
    resp = simulate(req).model_dump()
    assert resp["switch"]["state"] == "not_switched"
    assert resp["switch"]["effective_at_ms"] is None
    assert resp["effective_period_ms"] == 10 and resp["effective_reserve_ms"] == 5


# ---------------------------------------------------------------- 校验
def test_validation_errors():
    with pytest.raises(Exception):
        ReplayRequest(
            period_ms=10, reserve_ms=20, alarm_deadline_ms=5, replay_end_ms=100, frames=[]
        )
    with pytest.raises(Exception):
        ReplayRequest(
            period_ms=10,
            reserve_ms=5,
            alarm_deadline_ms=5,
            replay_end_ms=100,
            frames=[],
            switch_at_ms=50,
            # 缺新周期/新预留
        )
    with pytest.raises(Exception):
        ReplayRequest(
            period_ms=10,
            reserve_ms=5,
            alarm_deadline_ms=5,
            replay_end_ms=100,
            frames=[_frame("x", "alarm", 0, 5), _frame("x", "image", 0, 5)],
        )


# ---------------------------------------------------------------- HTTP 端到端
def test_http_replay_and_docs_available():
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200

    payload = {
        "period_ms": 100,
        "reserve_ms": 40,
        "alarm_deadline_ms": 80,
        "replay_end_ms": 320,
        "frames": [
            {"id": "A1", "type": "alarm", "arrival_ms": 0, "busy_ms": 30},
            {"id": "I1", "type": "image", "arrival_ms": 40, "busy_ms": 50},
        ],
    }
    r = client.post("/replay", json=payload)
    assert r.status_code == 200
    data = r.json()
    fr = _by_id(data)
    assert (fr["A1"]["start_ms"], fr["A1"]["end_ms"]) == (0, 30)
    assert (fr["I1"]["start_ms"], fr["I1"]["end_ms"]) == (40, 90)

    # 非法请求返回 422
    bad = dict(payload, reserve_ms=101)
    assert client.post("/replay", json=bad).status_code == 422
