"""时隙方案的持久化与并发控制。

用 SQLite 做唯一事实来源：

- ``slot_config`` 单行表保存首次成功登记时确定的出口配置（周期与告警预留）；
- ``line_plans`` 每产线一行，保存整组已生效方案（JSON）；
- 每次登记在 ``BEGIN IMMEDIATE`` 事务内完成：先锁库、再读其他产线、求解、写入。
  线程内另有可重入锁；跨线程/跨进程（多 uvicorn worker）都由 SQLite 写事务串行化，
  因此并发登记不会基于同一份旧快照各自成功而造成时隙冲突。
- 无解时事务回滚，原登记保持不动。
- 数据库文件落在磁盘（Docker Compose 中挂卷），重启后按产线查询仍可用。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .slot_models import AlarmFlowSpec, LinePlan, SelectedSlot, SlotConfig, StartPoint
from .slot_solver import FixedFlow, SolveFailure, solve_plan

DEFAULT_DB_PATH = "data/slots.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS slot_config (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    period_ms   INTEGER NOT NULL CHECK (period_ms >= 1),
    reserve_ms  INTEGER NOT NULL CHECK (reserve_ms >= 0),
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS line_plans (
    line_id     TEXT PRIMARY KEY,
    plan_json   TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


class RegistrationError(Exception):
    """业务层登记失败（可映射为 HTTP 状态码）。"""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class RegistrationOutcome:
    plan: LinePlan
    created: bool


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class SlotStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path or os.environ.get("SLOT_DB_PATH", DEFAULT_DB_PATH))
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        """幂等初始化；多 worker 同时首次启动时对同一个新库竞争，需重试。

        ``PRAGMA journal_mode = WAL`` 切换模式会短暂需要排他锁，且不走普通的
        busy handler，因此这里显式重试并退避，避免某个 worker 启动失败。
        """
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(40):
            conn = self._connect()
            try:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                if str(mode).lower() != "wal":
                    conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(_SCHEMA)
                return
            except sqlite3.OperationalError as exc:
                last_error = exc
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                time.sleep(min(0.05 * (attempt + 1), 0.5))
            finally:
                conn.close()
        assert last_error is not None
        raise last_error

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None：事务边界由本模块显式控制（BEGIN IMMEDIATE / COMMIT）。
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    @contextmanager
    def _read_conn(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------ 读
    def get_config(self) -> SlotConfig | None:
        with self._lock, self._read_conn() as conn:
            row = conn.execute("SELECT period_ms, reserve_ms FROM slot_config WHERE id = 1").fetchone()
        if row is None:
            return None
        return SlotConfig(period_ms=row[0], reserve_ms=row[1])

    @staticmethod
    def _row_to_plan(line_id: str, plan_json: str, updated_at: str) -> LinePlan:
        raw = json.loads(plan_json)
        return LinePlan(
            line_id=line_id,
            period_ms=raw["period_ms"],
            reserve_ms=raw["reserve_ms"],
            flows=[
                SelectedSlot(
                    flow_id=f["flow_id"],
                    busy_ms=f["busy_ms"],
                    repeat_every_periods=f["repeat_every_periods"],
                    start_point_index=f["start_point_index"],
                    start_point=StartPoint(
                        first_period=f["start_point"]["first_period"],
                        offset_ms=f["start_point"]["offset_ms"],
                    ),
                    first_start_ms=f["first_start_ms"],
                    first_end_ms=f["first_end_ms"],
                    reserve_start_ms=f["reserve_start_ms"],
                    reserve_end_ms=f["reserve_end_ms"],
                )
                for f in raw["flows"]
            ],
            updated_at=updated_at,
        )

    def get_line(self, line_id: str) -> LinePlan | None:
        with self._lock, self._read_conn() as conn:
            row = conn.execute(
                "SELECT plan_json, updated_at FROM line_plans WHERE line_id = ?", (line_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_plan(line_id, row[0], row[1])

    def list_lines(self) -> list[LinePlan]:
        with self._lock, self._read_conn() as conn:
            rows = conn.execute(
                "SELECT line_id, plan_json, updated_at FROM line_plans ORDER BY line_id"
            ).fetchall()
        return [self._row_to_plan(line_id, plan_json, updated_at) for line_id, plan_json, updated_at in rows]

    # ------------------------------------------------------------ 写
    def register(
        self,
        line_id: str,
        flows: list[AlarmFlowSpec],
        period_ms: int | None,
        reserve_ms: int | None,
    ) -> RegistrationOutcome:
        """登记/替换一条产线；整组有解才落库，否则抛 RegistrationError 并保留原登记。"""
        with self._lock:
            conn = self._connect()
            try:
                # 立刻拿写锁：并发登记在此排队，保证“读其他产线 + 求解 + 写入”原子。
                conn.execute("BEGIN IMMEDIATE")

                cfg_row = conn.execute(
                    "SELECT period_ms, reserve_ms FROM slot_config WHERE id = 1"
                ).fetchone()
                if cfg_row is None:
                    if period_ms is None or reserve_ms is None:
                        raise RegistrationError(
                            400, "出口尚未登记过，首次登记必须同时提供 period_ms 与 reserve_ms"
                        )
                    if period_ms < 1 or reserve_ms < 0 or reserve_ms > period_ms:
                        raise RegistrationError(
                            422,
                            "出口配置非法：需满足 period_ms >= 1 且 0 <= reserve_ms <= period_ms",
                        )
                    config = SlotConfig(period_ms=period_ms, reserve_ms=reserve_ms)
                else:
                    config = SlotConfig(period_ms=cfg_row[0], reserve_ms=cfg_row[1])
                    if period_ms is not None and (period_ms, reserve_ms) != (
                        config.period_ms,
                        config.reserve_ms,
                    ):
                        raise RegistrationError(
                            409,
                            "出口配置已由首次登记确定，本次提供 "
                            f"period_ms={period_ms}, reserve_ms={reserve_ms}，"
                            f"与已确定值 period_ms={config.period_ms}, reserve_ms={config.reserve_ms} 不一致",
                        )

                rows = conn.execute(
                    "SELECT line_id, plan_json FROM line_plans WHERE line_id <> ?", (line_id,)
                ).fetchall()
                fixed_flows: list[FixedFlow] = []
                for other_line, plan_json in rows:
                    raw = json.loads(plan_json)
                    for f in raw["flows"]:
                        fixed_flows.append(
                            FixedFlow(
                                flow_id=f"{other_line}/{f['flow_id']}",
                                busy_ms=f["busy_ms"],
                                repeat_every_periods=f["repeat_every_periods"],
                                first_period=f["start_point"]["first_period"],
                                offset_ms=f["start_point"]["offset_ms"],
                            )
                        )

                selected, failure = solve_plan(
                    flows,
                    config.period_ms,
                    config.reserve_ms,
                    fixed_flows=fixed_flows,
                )
                if selected is None:
                    assert failure is not None
                    raise RegistrationError(409, self._failure_detail(failure))

                now = _now_iso()
                if cfg_row is None:
                    conn.execute(
                        "INSERT INTO slot_config (id, period_ms, reserve_ms, created_at) VALUES (1, ?, ?, ?)",
                        (config.period_ms, config.reserve_ms, now),
                    )
                plan = LinePlan(
                    line_id=line_id,
                    period_ms=config.period_ms,
                    reserve_ms=config.reserve_ms,
                    flows=selected,
                    updated_at=now,
                )
                exists = conn.execute(
                    "SELECT 1 FROM line_plans WHERE line_id = ?", (line_id,)
                ).fetchone() is not None
                conn.execute(
                    "INSERT INTO line_plans (line_id, plan_json, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(line_id) DO UPDATE SET plan_json = excluded.plan_json, "
                    "updated_at = excluded.updated_at",
                    (line_id, plan.model_dump_json(), now),
                )
                conn.execute("COMMIT")
                return RegistrationOutcome(plan=plan, created=not exists)
            except RegistrationError:
                conn.execute("ROLLBACK")
                raise
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()

    @staticmethod
    def _failure_detail(failure: SolveFailure) -> str:
        if failure.flow_id is not None:
            return f"无解：{failure.reason}"
        return f"无解：{failure.reason}；未修改任何登记"
