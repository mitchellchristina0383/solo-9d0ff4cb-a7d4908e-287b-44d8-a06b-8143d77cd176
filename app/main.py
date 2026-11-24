"""FastAPI 入口：通过 /docs 可直接发起重放与时隙登记。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .models import ReplayRequest, ReplayResponse
from .replay import simulate
from .slot_api import router as slot_router
from .slot_store import SlotStore


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # 时隙登记状态持久化在 SQLite；路径可由 SLOT_DB_PATH 覆盖（Docker Compose 挂卷）。
    app.state.slot_store = SlotStore(os.environ.get("SLOT_DB_PATH"))
    yield


app = FastAPI(
    title="交换机出口 告警/相机图像 重放试算 API",
    description=(
        "产线告警与相机图像共用交换机出口的重放仿真，以及产线告警时隙登记。\n\n"
        "重放试算（/replay）：\n\n"
        "- 时间一律整数毫秒，时间轴从 0 开始，仿真区间 `[0, replay_end_ms)`。\n"
        "- 每周期前段（长度 reserve_ms）只发告警，其余时间只发图像。\n"
        "- 出口一次只发一帧，整帧必须完全落在本类时段内才会开始发送。\n"
        "- 同类帧按到达顺序发送（同时到达按请求中的先后）。\n"
        "- 可在指定时刻换一套周期/预留：到点若正在发帧则等其发完再切换，排队帧保留，"
        "新周期从实际生效时刻起算。\n"
        "- 告警从到达计时，发完即停止；未发完算到重放终点；耗时严格大于告警时限即超时。\n\n"
        "时隙登记（/lines/{line_id}/slots）：\n\n"
        "- 首次成功登记确定出口周期与告警预留，后续产线共用；之后提供不同值返回 409。\n"
        "- 每次登记/替换一条产线的 1~6 条告警流：流标识、占线时长、重复间隔（周期）、"
        "1~6 个可用起点（首发周期号 + 周期内偏移）。\n"
        "- 每条流选一个起点：整帧落在告警预留段，同一周期内帧互不重叠；"
        "求解用同余（CRT）判断周期序列是否相交，不逐周期展开。\n"
        "- 整组全部有解才生效；无解返回 409 并保留原登记。其他产线时隙原位保留。\n"
        "- 方案持久化在 SQLite，重启后仍可按产线查询。"
    ),
    version="1.1.0",
    lifespan=_lifespan,
)


_EXAMPLE_PAYLOAD = {
    "period_ms": 100,
    "reserve_ms": 40,
    "alarm_deadline_ms": 80,
    "replay_end_ms": 320,
    "frames": [
        {"id": "A1", "type": "alarm", "arrival_ms": 0, "busy_ms": 30},
        {"id": "A2", "type": "alarm", "arrival_ms": 5, "busy_ms": 60},
        {"id": "I1", "type": "image", "arrival_ms": 10, "busy_ms": 50},
        {"id": "A3", "type": "alarm", "arrival_ms": 120, "busy_ms": 25},
        {"id": "I2", "type": "image", "arrival_ms": 130, "busy_ms": 70},
    ],
    "switch_at_ms": 150,
    "new_period_ms": 120,
    "new_reserve_ms": 60,
}


@app.get("/health", tags=["运维"], summary="健康检查")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/replay",
    response_model=ReplayResponse,
    tags=["重放"],
    summary="执行一次重放试算",
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "examples": {
                        "含换配的完整示例": {"value": _EXAMPLE_PAYLOAD},
                        "不换配（最简）": {
                            "value": {
                                "period_ms": 100,
                                "reserve_ms": 40,
                                "alarm_deadline_ms": 80,
                                "replay_end_ms": 200,
                                "frames": [
                                    {"type": "alarm", "arrival_ms": 0, "busy_ms": 30},
                                    {"type": "image", "arrival_ms": 0, "busy_ms": 50},
                                ],
                            }
                        },
                    }
                }
            }
        }
    },
)
def replay(req: ReplayRequest) -> ReplayResponse:
    """按请求参数仿真，返回实际切换时刻、各帧发送起止、终点状态与超时告警。"""
    return simulate(req)


app.include_router(slot_router)
