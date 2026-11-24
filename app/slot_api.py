"""时隙登记的相关路由。"""

from __future__ import annotations

import os
import threading
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request

from .slot_models import (
    LinePlan,
    SlotConfig,
    SlotListResponse,
    SlotRegistrationRequest,
    SlotRegistrationResponse,
)
from .slot_store import RegistrationError, SlotStore

router = APIRouter(tags=["时隙登记"])

_store_lock = threading.Lock()

LineId = Annotated[
    str,
    Path(min_length=1, max_length=64, description="产线标识（1~64 字符）"),
]


def get_store(request: Request) -> SlotStore:
    """取应用级单例；lifespan 已初始化，未初始化时兜底创建（供未启动 lifespan 的调用方）。"""
    store = getattr(request.app.state, "slot_store", None)
    if store is None:
        with _store_lock:
            store = getattr(request.app.state, "slot_store", None)
            if store is None:
                store = SlotStore(os.environ.get("SLOT_DB_PATH"))
                request.app.state.slot_store = store
    return store


_FIRST_LINE_EXAMPLE = {
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
}

_REPLACE_EXAMPLE = {
    "flows": [
        {
            "flow_id": "A1",
            "busy_ms": 20,
            "repeat_every_periods": 1,
            "start_points": [
                {"first_period": 0, "offset_ms": 20},
                {"first_period": 0, "offset_ms": 0},
            ],
        },
        {
            "flow_id": "A2",
            "busy_ms": 15,
            "repeat_every_periods": 1,
            "start_points": [
                {"first_period": 0, "offset_ms": 20},
                {"first_period": 0, "offset_ms": 0},
            ],
        },
    ]
}

_CONCURRENT_LINE_EXAMPLE = {
    "flows": [
        {
            "flow_id": "B1",
            "busy_ms": 5,
            "repeat_every_periods": 1,
            "start_points": [{"first_period": 0, "offset_ms": 35}],
        }
    ]
}


@router.post(
    "/lines/{line_id}/slots",
    response_model=SlotRegistrationResponse,
    summary="登记或替换一条产线的告警流时隙",
    responses={
        400: {"description": "首次登记但未提供出口周期/预留时长"},
        409: {"description": "无解或与出口已确定配置冲突；原登记保持不变"},
    },
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "examples": {
                        "首次登记（确定周期与告警预留）": {"value": _FIRST_LINE_EXAMPLE},
                        "整组改选（替换已有产线，其他产线保持原位）": {"value": _REPLACE_EXAMPLE},
                        "拥挤的竞争者（可用来在 /docs 里点两次制造并发争用）": {
                            "value": _CONCURRENT_LINE_EXAMPLE
                        },
                    }
                }
            }
        }
    },
)
def register_line_slots(
    line_id: LineId,
    body: SlotRegistrationRequest,
    request: Request,
) -> SlotRegistrationResponse:
    """为整组 1~6 条告警流各选一个起点。

    - 首次成功登记时确定出口周期 ``period_ms`` 与告警预留 ``reserve_ms``，后续共用；
    - 每次登记/替换整条产线；整组全部有解才生效，无解保留原登记（409）；
    - 其他产线的时隙原位保留，并发登记由 SQLite 写事务串行化。
    """
    store = get_store(request)
    try:
        outcome = store.register(
            line_id=line_id,
            flows=body.flows,
            period_ms=body.period_ms,
            reserve_ms=body.reserve_ms,
        )
    except RegistrationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return SlotRegistrationResponse(
        line_id=outcome.plan.line_id,
        period_ms=outcome.plan.period_ms,
        reserve_ms=outcome.plan.reserve_ms,
        flows=outcome.plan.flows,
        updated_at=outcome.plan.updated_at,
        applied=True,
        created=outcome.created,
    )


@router.get(
    "/lines/{line_id}/slots",
    response_model=LinePlan,
    summary="查询一条产线当前生效的时隙方案",
    responses={404: {"description": "该产线尚未登记"}},
)
def get_line_slots(line_id: LineId, request: Request) -> LinePlan:
    plan = get_store(request).get_line(line_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"产线 {line_id} 尚未登记时隙")
    return plan


@router.get("/slots", response_model=SlotListResponse, summary="出口配置与全部产线时隙总览")
def list_slots(request: Request) -> SlotListResponse:
    store = get_store(request)
    config = store.get_config()
    return SlotListResponse(
        config=SlotConfig(period_ms=config.period_ms, reserve_ms=config.reserve_ms)
        if config is not None
        else None,
        lines=store.list_lines(),
    )
