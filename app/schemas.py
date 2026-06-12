from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl


Direction = Literal["利多", "利空", "混合", "中性", "不确定"]
TimeHorizon = Literal["0-24小时", "2-7天", "1-4周", "1个月以上"]
EventType = Literal[
    "新品供给",
    "掉落与容器机制",
    "赛事商店与贴纸",
    "交易与市场规则",
    "地图收藏品与纪念品",
    "武器与玩法平衡",
    "职业与赛事热度",
    "社区热点",
    "平台政策",
    "常规更新",
    "其他",
]
ImpactScope = Literal["个别饰品", "单一品类", "多品类", "全市场机制"]


class CollectedItem(BaseModel):
    """所有采集器必须输出的统一结构。

    先统一字段再进入业务流水线，可以把平台 API 变化限制在采集器内部，避免去重、AI
    和页面层同时适配不同来源格式。
    """

    external_id: str
    url: str
    title: str
    body: str = ""
    author: str | None = None
    published_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class PollResult(BaseModel):
    """一次来源轮询的结果，页面监控可额外返回新的正文指纹。"""

    items: list[CollectedItem] = Field(default_factory=list)
    source_fingerprint: str | None = None
    source_snapshot: str | None = None
    baseline_only: bool = False


class AffectedAsset(BaseModel):
    """被事件影响的饰品范围及其独立方向。"""

    category: str
    names: list[str] = Field(default_factory=list)
    direction: Direction
    reason: str


class MarketAnalysis(BaseModel):
    """AI 输出的严格契约，字段缺失时拒绝入库而不是猜测补值。"""

    is_market_relevant: bool
    event_type: EventType
    summary_zh: str
    verified_facts: list[str] = Field(default_factory=list)
    entities: dict[str, list[str]] = Field(default_factory=dict)
    affected_assets: list[AffectedAsset] = Field(default_factory=list)
    direction: Direction
    impact_strength: int = Field(ge=1, le=5)
    time_horizon: TimeHorizon
    market_mechanisms: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    impact_scope: ImpactScope = "单一品类"
    market_relevance_reason: str = ""
    market_signal_tags: list[str] = Field(default_factory=list)
    merge_keys: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class EventMergeDecision(BaseModel):
    """事件归并判定结果。

    单独定义结构化输出，是为了让事件归并和市场影响分析分别承担不同职责：
    一个负责“是不是同一事件”，另一个负责“会不会影响市场”。两者混在一起时，
    模型很容易因为标题相似就误判为同一事件。
    """

    same_event: bool
    confidence: float = Field(ge=0, le=1)
    reason: str


class EventMergeCandidate(BaseModel):
    """传给归并模型的候选事件摘要。"""

    event_id: int
    title: str
    summary: str
    event_type: str
    direction: str
    impact_strength: int
    earliest_published_at: datetime
    latest_published_at: datetime
    evidence_count: int
    verified_facts: list[str] = Field(default_factory=list)
    entities: dict[str, list[str]] = Field(default_factory=dict)
    affected_assets: list[dict[str, Any]] = Field(default_factory=list)
    sample_titles: list[str] = Field(default_factory=list)


class EventListItem(BaseModel):
    id: int
    title: str
    summary: str
    event_type: str
    direction: str
    impact_strength: int
    time_horizon: str
    confidence: float
    importance_score: int
    alert_level: str
    affected_assets: list[dict[str, Any]]
    first_seen_at: datetime
    last_seen_at: datetime

    model_config = {"from_attributes": True}


class DailyReportItem(BaseModel):
    """日报查询接口的稳定响应结构，避免把 SQLAlchemy 内部状态暴露给客户端。"""

    id: int
    report_date: str
    content_markdown: str
    event_ids: list[int]
    sent_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ManualUrlRequest(BaseModel):
    """人工补录仅接收 URL，正文仍由服务端抓取，避免伪造来源证据。"""

    url: HttpUrl
    title: str | None = None
