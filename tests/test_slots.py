"""时隙登记：求解器、持久化、并发与 HTTP 端到端自测。"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.slot_models import AlarmFlowSpec, StartPoint
from app.slot_solver import FixedFlow, flows_conflict, solve_plan
from app.slot_store import RegistrationError, SlotStore


def _flow(fid: str, busy: int, repeat: int, points: list[tuple[int, int]]) -> AlarmFlowSpec:
    return AlarmFlowSpec(
        flow_id=fid,
        busy_ms=busy,
        repeat_every_periods=repeat,
        start_points=[StartPoint(first_period=p, offset_ms=o) for p, o in points],
    )


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("SLOT_DB_PATH", str(tmp_path / "slots.db"))
    return TestClient(app)


# ------------------------------------------------------------------ 求解器
def test_solver_picks_earliest_non_overlapping_points():
    flows = [
        _flow("A1", 20, 1, [(0, 0), (0, 20)]),
        _flow("A2", 15, 1, [(0, 0), (0, 20)]),
    ]
    selected, failure = solve_plan(flows, period_ms=100, reserve_ms=40)
    assert failure is None
    by_id = {s.flow_id: s for s in selected}
    # A1 首选 (0,0)：[0,20)；A2 的 (0,0) 重叠，退到 (0,20)：[20,35) <= 40
    assert (by_id["A1"].start_point.first_period, by_id["A1"].start_point.offset_ms) == (0, 0)
    assert (by_id["A2"].start_point.first_period, by_id["A2"].start_point.offset_ms) == (0, 20)
    assert by_id["A2"].first_start_ms == 20 and by_id["A2"].first_end_ms == 35
    # 结果按请求顺序
    assert [s.flow_id for s in selected] == ["A1", "A2"]


def test_solver_rejects_points_that_do_not_fit_reserve():
    flows = [_flow("A1", 30, 1, [(0, 20)])]  # 20+30 > 40
    selected, failure = solve_plan(flows, period_ms=100, reserve_ms=40)
    assert selected is None and failure is not None and failure.flow_id == "A1"


def test_solver_uses_crt_to_allow_non_meeting_repeats():
    """重复间隔不同的两条流若周期序列永不相交，可共用同一周期内偏移。"""
    flows = [
        _flow("A1", 30, 2, [(0, 0)]),  # 周期 0,2,4,...
        _flow("A2", 30, 2, [(1, 0)]),  # 周期 1,3,5,... 与 A1 永不同周期
    ]
    selected, failure = solve_plan(flows, period_ms=100, reserve_ms=40)
    assert failure is None and len(selected) == 2
    fixed = [
        FixedFlow(s.flow_id, s.busy_ms, s.repeat_every_periods,
                  s.start_point.first_period, s.start_point.offset_ms)
        for s in selected
    ]
    assert not flows_conflict(fixed[0], fixed[1])


def test_solver_detects_meeting_repeats_and_says_no_solution():
    flows = [
        _flow("A1", 30, 2, [(0, 0)]),  # 周期 0,2,4,...
        _flow("A2", 30, 4, [(2, 0)]),  # 周期 2,6,10,... 与 A1 在 2,6,... 相交
    ]
    selected, failure = solve_plan(flows, period_ms=100, reserve_ms=40)
    assert selected is None
    assert failure is not None and failure.flow_id is None


def test_solver_backtracks_to_find_complete_group():
    """整组求解：A1 的首选起点会挤掉 A2，必须回退改选 A1，而不是只按照请求顺序贪心。"""
    flows = [
        _flow("A1", 10, 1, [(0, 0), (0, 30)]),
        _flow("A2", 10, 1, [(0, 0)]),
    ]
    selected, failure = solve_plan(flows, period_ms=100, reserve_ms=40)
    assert failure is None
    by_id = {s.flow_id: s for s in selected}
    assert by_id["A1"].start_point.offset_ms == 30
    assert by_id["A2"].start_point.offset_ms == 0


def test_solver_backtracks_with_repeat_sequences():
    """A1 间隔 2（周期 0,2,4…），A2 间隔 4 且首发周期 2 -> 在周期 2 相交，偏移必须错开。"""
    flows = [
        _flow("A1", 20, 2, [(0, 0), (0, 20)]),
        _flow("A2", 20, 4, [(2, 0)]),
    ]
    selected, failure = solve_plan(flows, period_ms=100, reserve_ms=40)
    assert failure is None
    by_id = {s.flow_id: s for s in selected}
    assert by_id["A1"].start_point.offset_ms == 20
    assert by_id["A2"].start_point.offset_ms == 0


def test_solver_respects_other_lines_slots():
    fixed = [FixedFlow("line1/A1", busy_ms=30, repeat_every_periods=1, first_period=0, offset_ms=0)]
    flows = [_flow("B1", 5, 1, [(0, 0), (0, 35)])]
    selected, failure = solve_plan(flows, period_ms=100, reserve_ms=40, fixed_flows=fixed)
    assert failure is None
    assert selected[0].start_point.offset_ms == 35


def test_solver_reserve_must_hold_whole_frame_and_gcd_rule():
    # gcd(3, 5) = 1：任意 first_period 都会相交 -> 偏移必须错开
    flows = [
        _flow("A1", 20, 3, [(0, 0)]),
        _flow("A2", 20, 5, [(1, 0)]),
    ]
    selected, failure = solve_plan(flows, period_ms=50, reserve_ms=40)
    assert selected is None and failure is not None

    flows[1] = _flow("A2", 20, 5, [(1, 20)])
    selected, failure = solve_plan(flows, period_ms=50, reserve_ms=40)
    assert failure is None and len(selected) == 2


def test_solver_matches_per_period_bruteforce_on_random_cases():
    """随机交叉验证：把解与“逐周期展开”的暴力判定对照，确认 CRT 判据既不漏也不误。"""

    import itertools
    import random

    def conflict(a, b, horizon=80):
        for period_no in range(horizon):
            def active(f):
                return period_no >= f[0] and (period_no - f[0]) % f[1] == 0

            if active(a) and active(b) and max(a[3], b[3]) < min(a[3] + a[4], b[3] + b[4]):
                return True
        return False

    def brute(flows, reserve, fixed):
        per_flow = []
        for f in flows:
            candidates = [
                (i, p.first_period, p.offset_ms)
                for i, p in enumerate(f.start_points)
                if p.offset_ms + f.busy_ms <= reserve
            ]
            if not candidates:
                return None
            per_flow.append(candidates)
        for combo in itertools.product(*per_flow):
            ok = True
            for i, (_, fp, off) in enumerate(combo):
                fi = flows[i]
                entry = (fp, fi.repeat_every_periods, 0, off, fi.busy_ms)
                if any(
                    conflict(entry, (fx.first_period, fx.repeat_every_periods, 0,
                                     fx.offset_ms, fx.busy_ms))
                    for fx in fixed
                ):
                    ok = False
                    break
                for j in range(i + 1, len(combo)):
                    fj = flows[j]
                    _, fp2, off2 = combo[j]
                    if conflict(entry, (fp2, fj.repeat_every_periods, 0, off2, fj.busy_ms)):
                        ok = False
                        break
                if not ok:
                    break
            if ok:
                return combo
        return None

    rng = random.Random(20261124)
    for _ in range(300):
        period_ms = rng.choice([20, 30, 50])
        reserve_ms = rng.choice([10, 15, 20, 25])
        fixed = [
            FixedFlow(
                f"X{k}",
                busy_ms=rng.randint(1, min(6, reserve_ms)),
                repeat_every_periods=rng.randint(1, 3),
                first_period=rng.randint(0, 2),
                offset_ms=rng.randint(0, reserve_ms - 1),
            )
            for k in range(rng.randint(0, 3))
        ]
        fixed = [f for f in fixed if f.offset_ms + f.busy_ms <= reserve_ms]
        flows = []
        for k in range(rng.randint(1, 3)):
            busy = rng.randint(1, min(6, reserve_ms))
            points = {
                (rng.randint(0, 3), rng.randint(0, reserve_ms - busy))
                for _ in range(rng.randint(1, 3))
            }
            flows.append(
                AlarmFlowSpec(
                    flow_id=f"F{k}",
                    busy_ms=busy,
                    repeat_every_periods=rng.randint(1, 4),
                    start_points=[StartPoint(first_period=a, offset_ms=b) for a, b in sorted(points)],
                )
            )

        selected, failure = solve_plan(flows, period_ms, reserve_ms, fixed_flows=fixed)
        expected = brute(flows, reserve_ms, fixed)
        assert (selected is None) == (expected is None), (
            period_ms, reserve_ms, [f.model_dump() for f in flows], fixed, expected
        )
        if selected is None:
            assert failure is not None
            continue
        for slot in selected:
            assert slot.start_point.offset_ms + slot.busy_ms <= reserve_ms
        # 解本身必须两两不冲突，且与其他产线不冲突
        entries = [
            (s.start_point.first_period, s.repeat_every_periods, 0,
             s.start_point.offset_ms, s.busy_ms)
            for s in selected
        ]
        fixed_entries = [
            (f.first_period, f.repeat_every_periods, 0, f.offset_ms, f.busy_ms) for f in fixed
        ]
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                assert not conflict(entries[i], entries[j])
            for other in fixed_entries:
                assert not conflict(entries[i], other)


# ------------------------------------------------------------------ 持久化
def test_store_first_registration_sets_config_and_later_must_match(tmp_path):
    store = SlotStore(tmp_path / "slots.db")
    assert store.get_config() is None
    with pytest.raises(RegistrationError) as exc:
        store.register("line1", [_flow("A1", 10, 1, [(0, 0)])], None, None)
    assert exc.value.status_code == 400

    outcome = store.register("line1", [_flow("A1", 10, 1, [(0, 0)])], 100, 40)
    assert outcome.created is True
    assert store.get_config().period_ms == 100

    with pytest.raises(RegistrationError) as exc:
        store.register("line2", [_flow("B1", 5, 1, [(0, 10)])], 120, 50)
    assert exc.value.status_code == 409

    # 省略配置，沿用已确定值
    outcome2 = store.register("line2", [_flow("B1", 5, 1, [(0, 10)])], None, None)
    assert outcome2.plan.period_ms == 100 and outcome2.created is True


def test_store_no_solution_keeps_previous_plan(tmp_path):
    store = SlotStore(tmp_path / "slots.db")
    store.register("line1", [_flow("A1", 10, 1, [(0, 0)])], 100, 40)
    before = store.get_line("line1")

    with pytest.raises(RegistrationError) as exc:
        store.register("line1", [_flow("A1", 10, 1, [(0, 35)])], None, None)  # 35+10 > 40
    assert exc.value.status_code == 409
    after = store.get_line("line1")
    assert after.model_dump() == before.model_dump()


def test_store_replacement_is_atomic_and_other_lines_untouched(tmp_path):
    store = SlotStore(tmp_path / "slots.db")
    store.register("line1", [_flow("A1", 10, 1, [(0, 0)])], 100, 40)
    store.register("line2", [_flow("B1", 10, 1, [(0, 30)])], None, None)
    line2_before = store.get_line("line2").model_dump()

    # line1 改选到 (0, 30) 会与 line2 冲突 -> 整组失败，两边都不动
    with pytest.raises(RegistrationError):
        store.register("line1", [_flow("A1", 10, 1, [(0, 30)])], None, None)
    assert store.get_line("line1").flows[0].start_point.offset_ms == 0
    assert store.get_line("line2").model_dump() == line2_before

    # 合法改选成功（[20,30) 与 line2 的 [30,40) 不重叠），line2 原位不动
    out = store.register("line1", [_flow("A1", 10, 1, [(0, 20)])], None, None)
    assert out.created is False
    assert out.plan.flows[0].start_point.offset_ms == 20
    assert store.get_line("line2").model_dump() == line2_before


def test_store_persists_across_instances(tmp_path):
    db = tmp_path / "slots.db"
    SlotStore(db).register("line1", [_flow("A1", 20, 1, [(0, 0), (0, 20)])], 100, 40)
    reopened = SlotStore(db)
    plan = reopened.get_line("line1")
    assert plan is not None
    assert plan.flows[0].start_point.offset_ms == 0
    assert reopened.get_config().period_ms == 100


def test_store_rejects_invalid_first_config_and_bad_offsets(tmp_path):
    store = SlotStore(tmp_path / "slots.db")
    with pytest.raises(RegistrationError) as exc:
        # 首次登记时 reserve > period 无法经 API 进入，但存储层也需自卫
        store.register("line1", [_flow("A1", 5, 1, [(0, 0)])], 100, 400)
    assert exc.value.status_code == 422
    assert store.get_config() is None


def test_concurrent_store_initialization_on_fresh_db(tmp_path):
    """多 worker 同时对同一个新库做首次初始化不能因 WAL 切换锁失败。"""
    db = tmp_path / "slots.db"
    errors: list[BaseException] = []

    def init() -> None:
        try:
            SlotStore(db)
        except BaseException as exc:  # noqa: BLE001 - 测试需要记录任何失败
            errors.append(exc)

    threads = [threading.Thread(target=init) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert SlotStore(db).get_config() is None


def test_concurrent_registrations_never_overlap(tmp_path):
    """8 个线程同时登记不同产线，同一个预留段里只有空间够的方案能成功；
    最终所有生效方案两两不冲突。"""
    db = tmp_path / "slots.db"
    store = SlotStore(db)
    store.register("seed", [_flow("S", 5, 1, [(0, 0)])], 100, 40)

    results: list[tuple[str, bool]] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        # 每条流 10ms，独占一个 10ms 槽；预留 40ms 只有 3 个空槽（10/20/30）
        flow = _flow(f"F{i}", 10, 1, [(0, 10), (0, 20), (0, 30)])
        try:
            SlotStore(db).register(f"line{i}", [flow], None, None)
            ok = True
        except RegistrationError:
            ok = False
        with lock:
            results.append((f"line{i}", ok))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [name for name, ok in results if ok]
    assert len(winners) == 3, results
    plans = store.list_lines()
    fixed: list[FixedFlow] = []
    for plan in plans:
        for f in plan.flows:
            fixed.append(
                FixedFlow(
                    f"{plan.line_id}/{f.flow_id}",
                    f.busy_ms,
                    f.repeat_every_periods,
                    f.start_point.first_period,
                    f.start_point.offset_ms,
                )
            )
    for i in range(len(fixed)):
        for j in range(i + 1, len(fixed)):
            assert not flows_conflict(fixed[i], fixed[j])


# ------------------------------------------------------------------ HTTP
def test_http_slot_registration_flow_and_restart(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        assert client.get("/lines/line1/slots").status_code == 404

        r = client.post(
            "/lines/line1/slots",
            json={
                "period_ms": 100,
                "reserve_ms": 40,
                "flows": [
                    {
                        "flow_id": "A1",
                        "busy_ms": 20,
                        "repeat_every_periods": 1,
                        "start_points": [
                            {"first_period": 0, "offset_ms": 0},
                            {"first_period": 0, "offset_ms": 20},
                        ],
                    },
                    {
                        "flow_id": "A2",
                        "busy_ms": 15,
                        "repeat_every_periods": 1,
                        "start_points": [
                            {"first_period": 0, "offset_ms": 0},
                            {"first_period": 0, "offset_ms": 20},
                        ],
                    },
                ],
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["created"] is True and body["applied"] is True
        assert [f["flow_id"] for f in body["flows"]] == ["A1", "A2"]
        assert body["flows"][0]["start_point"] == {"first_period": 0, "offset_ms": 0}
        assert body["flows"][1]["start_point"] == {"first_period": 0, "offset_ms": 20}
        assert body["flows"][1]["first_start_ms"] == 20

        # 无解：A2 只有 30ms 起点但 busy=15 -> 30+15>40
        r = client.post(
            "/lines/line1/slots",
            json={
                "flows": [
                    {
                        "flow_id": "A1",
                        "busy_ms": 20,
                        "repeat_every_periods": 1,
                        "start_points": [{"first_period": 0, "offset_ms": 30}],
                    }
                ]
            },
        )
        assert r.status_code == 409
        # 原登记保留
        assert client.get("/lines/line1/slots").json()["flows"][0]["start_point"]["offset_ms"] == 0

        # 校验：7 条流、7 个起点、重复 flow_id、缺配置
        seven = {
            "flow_id": "X",
            "busy_ms": 1,
            "repeat_every_periods": 1,
            "start_points": [{"first_period": 0, "offset_ms": 0}],
        }
        r = client.post("/lines/tmp/slots", json={"flows": [seven] * 7})
        assert r.status_code == 422
        r = client.post(
            "/lines/tmp/slots",
            json={"flows": [{**seven, "start_points": [{"first_period": 0, "offset_ms": 0}] * 7}]},
        )
        assert r.status_code == 422
        r = client.post("/lines/tmp/slots", json={"flows": [seven, seven]})
        assert r.status_code == 422
        # 新产线省略出口配置：已有配置可沿用
        r = client.post(
            "/lines/tmp/slots",
            json={
                "flows": [
                    {"flow_id": "T1", "busy_ms": 5, "repeat_every_periods": 1,
                     "start_points": [{"first_period": 0, "offset_ms": 35}]}
                ]
            },
        )
        assert r.status_code == 200

        listing = client.get("/slots").json()
        assert listing["config"] == {"period_ms": 100, "reserve_ms": 40}
        assert [line["line_id"] for line in listing["lines"]] == ["line1", "tmp"]

    # 模拟重启：新 TestClient / 新 lifespan，数据库仍在
    with _client(tmp_path, monkeypatch) as client:
        r = client.get("/lines/line1/slots")
        assert r.status_code == 200
        assert r.json()["flows"][1]["start_point"]["offset_ms"] == 20


def test_http_concurrent_registration(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        client.post(
            "/lines/seed/slots",
            json={
                "period_ms": 100,
                "reserve_ms": 40,
                "flows": [
                    {"flow_id": "S", "busy_ms": 5, "repeat_every_periods": 1,
                     "start_points": [{"first_period": 0, "offset_ms": 0}]}
                ],
            },
        )
        statuses: dict[int, int] = {}

        def worker(i: int) -> None:
            r = client.post(
                f"/lines/c{i}/slots",
                json={
                    "flows": [
                        {"flow_id": "F", "busy_ms": 10, "repeat_every_periods": 1,
                         "start_points": [{"first_period": 0, "offset_ms": 10},
                                          {"first_period": 0, "offset_ms": 20},
                                          {"first_period": 0, "offset_ms": 30}]}
                    ]
                },
            )
            statuses[i] = r.status_code

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(statuses.values()) == [200, 200, 200, 409, 409, 409]
        lines = client.get("/slots").json()["lines"]
        assert len(lines) == 4  # seed + 3 个成功


def test_openapi_documents_slot_routes(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        spec = client.get("/openapi.json").json()
        assert "/lines/{line_id}/slots" in spec["paths"]
        assert set(spec["paths"]["/lines/{line_id}/slots"]) == {"post", "get"}
        assert "/slots" in spec["paths"]
        assert client.get("/docs").status_code == 200
