"""请求与响应数据模型（时间单位均为整数毫秒）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

FrameTypeName = Literal["alarm", "image"]
FrameStatus = Literal["sent", "in_progress", "queued", "not_arrived"]
SwitchState = Literal["not_requested", "switched", "not_switched"]


class FrameConfig(BaseModel):
    """一帧的输入参数。

    - ``id``：可选帧标识，缺省按输入顺序生成 F1、F2……
    - ``type``：``alarm`` 告警帧 / ``image`` 相机图像帧
    - ``arrival_ms``：到达时刻（相对重放起点 0）
    - ``busy_ms``：占用出口的发送时长（整帧），必须 >= 1
    """

    id: str | None = Field(default=None, description="帧标识；缺省时按输入顺序生成 F1、F2……")
    type: FrameTypeName = Field(description="alarm=告警帧，image=相机图像帧")
    arrival_ms: int = Field(ge=0, description="到达时刻（毫秒，>=0）")
    busy_ms: int = Field(ge=1, description="整帧占线时长（毫秒，>=1）")


class ReplayRequest(BaseModel):
    """重放请求。

    周期 = 告警预留时段 + 图像时段：每个周期前段（起点起 reserve_ms）只发告警，
    其余时间只发图像。出口同一时刻只发一帧，且整帧必须完全落在本类时段内。
    """

    period_ms: int = Field(ge=1, description="初始周期长度（毫秒，>=1）")
    reserve_ms: int = Field(ge=0, description="初始每周期告警预留时长（毫秒，0<=x<=周期）")
    frames: list[FrameConfig] = Field(default_factory=list, description="各帧的类别、到达时刻与占线时长")
    alarm_deadline_ms: int = Field(ge=0, description="告警时限：从到达计时，耗时超过该值即超时")
    replay_end_ms: int = Field(ge=1, description="重放终点（毫秒，>=1），仿真区间为 [0, replay_end_ms)")
    switch_at_ms: int | None = Field(
        default=None, ge=0, description="可选：计划换配时刻；到点若出口正在发帧，则等该帧发完再切换"
    )
    new_period_ms: int | None = Field(default=None, ge=1, description="换配后的新周期长度")
    new_reserve_ms: int | None = Field(default=None, ge=0, description="换配后的新告警预留时长")

    @model_validator(mode="after")
    def _check_cross_fields(self) -> "ReplayRequest":
        if self.reserve_ms > self.period_ms:
            raise ValueError("reserve_ms 不能大于 period_ms（告警预留不能超过整个周期）")

        switch_fields = (self.switch_at_ms, self.new_period_ms, self.new_reserve_ms)
        if any(v is not None for v in switch_fields):
            if self.switch_at_ms is None:
                raise ValueError("提供了新周期/新预留时长时，必须同时提供 switch_at_ms")
            if self.new_period_ms is None or self.new_reserve_ms is None:
                raise ValueError("指定 switch_at_ms 时，必须同时提供 new_period_ms 与 new_reserve_ms")
            if self.new_reserve_ms > self.new_period_ms:
                raise ValueError("new_reserve_ms 不能大于 new_period_ms")

        ids = [f.id for f in self.frames if f.id is not None]
        if len(ids) != len(set(ids)):
            raise ValueError("帧 id 不能重复")
        return self


class SwitchInfo(BaseModel):
    """换配结果。"""

    requested_at_ms: int | None = Field(description="计划换配时刻；未指定换配时为 null")
    effective_at_ms: int | None = Field(description="实际切换时刻；截至重放终点未切换则为 null")
    switched: bool = Field(description="重放结束前是否已实际切换")
    state: SwitchState = Field(
        description="not_requested=未要求换配；switched=已切换；not_switched=要求了但截至终点尚未切换"
    )


class FrameResult(BaseModel):
    """单帧仿真结果。"""

    id: str
    type: FrameTypeName
    arrival_ms: int = Field(description="到达时刻")
    busy_ms: int = Field(description="整帧占线时长")
    start_ms: int | None = Field(description="实际发送起点；未开始发送为 null")
    end_ms: int | None = Field(description="实际发送终点；未发完（含发送中跨终点）为 null")
    status: FrameStatus = Field(
        description=(
            "sent=已发完；in_progress=终点时仍在发送（未发完）；"
            "queued=已到达但排队未发；not_arrived=终点之后才到达"
        )
    )
    alarm_elapsed_ms: int | None = Field(
        description="告警计时（毫秒）：发完=发送终点-到达；未发完=重放终点-到达；非告警帧为 null"
    )
    timed_out: bool | None = Field(description="告警是否超时；非告警帧或未到达帧为 null")


class ReplayStats(BaseModel):
    """汇总统计。"""

    total: int
    sent: int
    in_progress: int
    queued: int
    not_arrived: int
    alarm_total: int = Field(description="重放终点前已到达的告警帧总数")
    alarm_timed_out: int = Field(description="其中超时的告警帧数")


class ReplayResponse(BaseModel):
    """重放响应。"""

    replay_end_ms: int
    switch: SwitchInfo
    initial_period_ms: int
    initial_reserve_ms: int
    effective_period_ms: int = Field(description="重放终点时刻实际生效的周期（未切换则等于初始周期）")
    effective_reserve_ms: int = Field(description="重放终点时刻实际生效的告警预留时长")
    frames: list[FrameResult] = Field(description="按请求中的输入顺序排列")
    stats: ReplayStats
