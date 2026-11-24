"""时隙登记 API 的请求 / 响应模型。

时间与偏移一律为整数毫秒。周期号从 0 开始：
起点 ``(first_period, offset_ms)`` 对应的绝对发送时刻为
``first_period * period_ms + offset_ms``。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_FLOWS_PER_LINE = 6
MAX_START_POINTS_PER_FLOW = 6


class StartPoint(BaseModel):
    """一条告警流的一个可用起点。

    - ``first_period``：首发周期号（0 起），从该周期开始发送首帧；
    - ``offset_ms``：周期内偏移（毫秒），整帧占 ``[offset, offset + busy_ms)``。
    """

    first_period: int = Field(ge=0, description="首发周期号（0 起）")
    offset_ms: int = Field(ge=0, description="周期内偏移（毫秒，>=0）")


class AlarmFlowSpec(BaseModel):
    """一条告警流的登记参数。"""

    flow_id: str = Field(min_length=1, max_length=64, description="流标识，同一请求内不可重复")
    busy_ms: int = Field(ge=1, description="整帧占线时长（毫秒，>=1）")
    repeat_every_periods: int = Field(ge=1, description="重复间隔，以周期计（>=1）")
    start_points: list[StartPoint] = Field(
        min_length=1,
        max_length=MAX_START_POINTS_PER_FLOW,
        description=f"可用起点，1~{MAX_START_POINTS_PER_FLOW} 个；求解时从中选一个",
    )

    @field_validator("flow_id")
    @classmethod
    def _strip_flow_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("flow_id 不能为空白")
        return value

    @model_validator(mode="after")
    def _check_start_points(self) -> "AlarmFlowSpec":
        keys = [(p.first_period, p.offset_ms) for p in self.start_points]
        if len(keys) != len(set(keys)):
            raise ValueError(f"流 {self.flow_id} 的起点不能重复")
        return self


class SlotRegistrationRequest(BaseModel):
    """登记或替换一条产线的告警流组。

    ``period_ms`` / ``reserve_ms`` 只在出口首次成功登记时生效，二者必须同时提供；
    此后所有产线共用这套配置，后续请求可省略，若提供则必须与已确定值一致。
    """

    period_ms: int | None = Field(
        default=None, ge=1, description="出口周期（毫秒）。首次登记必填；之后可省略或填相同值"
    )
    reserve_ms: int | None = Field(
        default=None, ge=0, description="每周期告警预留时长（毫秒）。首次登记必填；之后可省略或填相同值"
    )
    flows: list[AlarmFlowSpec] = Field(
        min_length=1,
        max_length=MAX_FLOWS_PER_LINE,
        description=f"本产线的告警流，1~{MAX_FLOWS_PER_LINE} 条；整组全部有解才生效",
    )

    @model_validator(mode="after")
    def _check_cross_fields(self) -> "SlotRegistrationRequest":
        if (self.period_ms is None) != (self.reserve_ms is None):
            raise ValueError("period_ms 与 reserve_ms 必须同时提供或同时省略")
        if (
            self.period_ms is not None
            and self.reserve_ms is not None
            and self.reserve_ms > self.period_ms
        ):
            raise ValueError("reserve_ms 不能大于 period_ms")
        ids = [f.flow_id for f in self.flows]
        if len(ids) != len(set(ids)):
            raise ValueError("同一产线的 flow_id 不能重复")
        return self


class SlotConfig(BaseModel):
    """出口级时隙配置（首次成功登记时确定，之后不可更改）。"""

    period_ms: int = Field(description="出口周期（毫秒）")
    reserve_ms: int = Field(description="每周期告警预留时长（毫秒）")


class SelectedSlot(BaseModel):
    """一条告警流最终选中的起点与首帧信息。"""

    flow_id: str
    busy_ms: int = Field(description="整帧占线时长（毫秒）")
    repeat_every_periods: int = Field(description="重复间隔（周期）")
    start_point_index: int = Field(ge=0, description="选中的起点在请求 start_points 中的下标（0 起）")
    start_point: StartPoint = Field(description="选中的起点")
    first_start_ms: int = Field(ge=0, description="首帧绝对发送起点 = first_period * period + offset")
    first_end_ms: int = Field(ge=0, description="首帧绝对发送终点 = first_start_ms + busy_ms")
    reserve_start_ms: int = Field(ge=0, description="首发周期告警预留段起点")
    reserve_end_ms: int = Field(ge=0, description="首发周期告警预留段终点（不含）")


class LinePlan(BaseModel):
    """一条产线当前生效的整组方案。"""

    line_id: str
    period_ms: int = Field(description="出口周期（毫秒）")
    reserve_ms: int = Field(description="每周期告警预留时长（毫秒）")
    flows: list[SelectedSlot] = Field(description="按请求顺序排列")
    updated_at: str = Field(description="最近一次成功登记的 UTC 时间（ISO 8601）")


class SlotRegistrationResponse(LinePlan):
    """登记成功响应。"""

    applied: bool = Field(default=True, description="整组分配是否生效（成功响应恒为 true）")
    created: bool = Field(description="true=新建产线；false=替换已有产线")


class SlotListResponse(BaseModel):
    """出口时隙总览。"""

    config: SlotConfig | None = Field(description="出口配置；尚无成功登记时为 null")
    lines: list[LinePlan] = Field(description="按 line_id 排序的已生效产线方案")
