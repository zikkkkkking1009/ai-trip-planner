"""数据模型：景点、行程、约束参数。

整个项目只有这几种核心结构，先读懂这个文件，再看求解器。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from cities import DEFAULT_CITY


class Spot(BaseModel):
    """一个景点（POI）。

    source_id: 抽取阶段的原始编号，用于追踪对齐结果
    lat/lon:   坐标（GCJ-02，和高德一致）
    stay_min:  建议停留时长（分钟）
    score:     收益分 = 评分 * 用户偏好权重，求解器最大化它
    ticket:    门票价格（元）
    open_h/close_h: 开放时间窗（小时，如 8.0~17.0）
    """
    source_id: int
    name: str
    lat: float
    lon: float
    stay_min: int = Field(ge=30, le=480)
    score: float = Field(ge=0)
    ticket: float = Field(ge=0, default=0)
    open_h: float = 8.0
    close_h: float = 18.0
    desc: str = ""               # 一句话介绍（LLM 抽取的 note / 演示数据预写）
    image: str = ""              # 高德 POI 实景图 URL（spot_media.json / 抽取时补充）
    intro: str = ""              # 高德景点介绍（类型/评分/人均，来自 place detail）


class Hotel(BaseModel):
    """住宿锚点：每天从这里出发、回到这里。不是行程条目，不占游玩时长。"""
    name: str
    lat: float
    lon: float
    desc: str = ""


class PlanRequest(BaseModel):
    """用户请求：一组景点 + 行程参数。

    city 参与实体对齐（高德 citylimit 搜索）与通勤查询，**调用方必须显式传**；
    默认值仅为兼容历史调用，不代表"可以随便用西安"（前端会强制带上当前城市）。
    """
    city: str = DEFAULT_CITY      # 见 backend/cities.py
    days: int = Field(ge=1, le=7)
    daily_start_h: float = 9.0   # 每天从酒店出发的时间
    daily_end_h: float = 18.0    # 每天必须回到酒店的时间
    budget: float | None = None  # 总预算（元），None = 不限
    spots: list[Spot]
    hotel: Hotel | None = None   # 住宿锚点（对话中可设定/更换）


class VisitedSpot(BaseModel):
    """行程中一个被安排的景点（带到达/离开时刻）。"""
    name: str
    arrive_h: float
    depart_h: float
    ticket: float
    desc: str = ""
    image: str = ""
    intro: str = ""


class DayPlan(BaseModel):
    """一天的安排。"""
    day: int
    spots: list[VisitedSpot]
    commute_min: float        # 当天纯通勤总时长
    cost: float               # 当天门票总花费
    active_min: float         # 当天游玩总时长


class UnplannedSpot(BaseModel):
    """没被排进行程的景点 + 原因。"""
    name: str
    reason: str  # "时间窗装不下" / "预算不足"


class PlanResult(BaseModel):
    """求解器的输出。"""
    city: str
    days: list[DayPlan]
    total_cost: float
    total_score: float
    unplanned: list[UnplannedSpot]
    check_report: dict        # 约束校验报告（见 constraint_check.py）
