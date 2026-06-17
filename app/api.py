import hmac
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings, get_settings
from app.database import get_db
from app.models import DailyReport, Event, EventEvidence, Source
from app.schemas import DailyReportItem, EventListItem, ManualUrlRequest
from app.services.manual import UnsafeUrlError, fetch_manual_item
from app.services.notifier import FeishuNotifier
from app.services.pipeline import IntelligencePipeline
from app.services.time import format_local_datetime, to_local_datetime


router = APIRouter()
templates = Jinja2Templates(directory=str(get_settings().templates_dir))
templates.env.filters["local_time"] = lambda value, fmt="%m-%d %H:%M": format_local_datetime(
    value,
    get_settings().timezone,
    fmt,
)


def _event_item_for_response(event: Event, settings: Settings) -> EventListItem:
    """把事件响应中的 UTC 时间转成用户配置时区。

    数据库存 UTC 是正确的，但 API 是给页面或人工排查看的，直接暴露 SQLite 的无时区 UTC 会造成 8 小时偏差。
    """

    item = EventListItem.model_validate(event)
    item.first_seen_at = to_local_datetime(item.first_seen_at, settings.timezone)
    item.last_seen_at = to_local_datetime(item.last_seen_at, settings.timezone)
    return item


def _daily_report_for_response(report: DailyReport, settings: Settings) -> DailyReportItem:
    item = DailyReportItem.model_validate(report)
    item.created_at = to_local_datetime(item.created_at, settings.timezone)
    item.sent_at = to_local_datetime(item.sent_at, settings.timezone)
    return item


def require_admin_token(
    x_admin_token: str = Header(default=""), settings: Settings = Depends(get_settings)
) -> None:
    """使用常量时间比较管理令牌，避免普通字符串比较泄露前缀匹配时间。"""

    if not settings.admin_token or not hmac.compare_digest(x_admin_token, settings.admin_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的管理令牌")


def _event_query(
    *,
    alert_level: str | None,
    event_type: str | None,
    direction: str | None,
    start_at: datetime | None,
):
    query = select(Event)
    if alert_level:
        query = query.where(Event.alert_level == alert_level)
    if event_type:
        query = query.where(Event.event_type == event_type)
    if direction:
        query = query.where(Event.direction == direction)
    if start_at:
        query = query.where(Event.first_seen_at >= start_at)
    return query.order_by(Event.importance_score.desc(), Event.first_seen_at.desc())


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/events", response_model=list[EventListItem])
def list_events(
    alert_level: str | None = None,
    event_type: str | None = None,
    direction: str | None = None,
    start_at: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[EventListItem]:
    """按核心维度查询事件，限制最大页长防止一次拉取全部证据。"""

    events = db.scalars(_event_query(
        alert_level=alert_level, event_type=event_type, direction=direction, start_at=start_at
    ).offset(offset).limit(limit)).all()
    return [_event_item_for_response(event, settings) for event in events]


@router.get("/api/events/{event_id}")
def event_detail(
    event_id: int,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    event = db.scalar(
        select(Event)
        .options(selectinload(Event.evidence).selectinload(EventEvidence.raw_item))
        .where(Event.id == event_id)
    )
    if not event:
        raise HTTPException(status_code=404, detail="事件不存在")
    return {
        "event": _event_item_for_response(event, settings),
        "verified_facts": event.verified_facts,
        "entities": event.entities,
        "market_mechanisms": event.market_mechanisms,
        "uncertainties": event.uncertainties,
        "evidence": [
            {
                "title": link.raw_item.title,
                "url": link.raw_item.canonical_url,
                "source": link.raw_item.source.name,
                "published_at": to_local_datetime(link.raw_item.published_at, settings.timezone),
            }
            for link in event.evidence
        ],
    }


@router.get("/api/reports/daily", response_model=list[DailyReportItem])
def daily_reports(
    limit: int = Query(default=30, ge=1, le=100),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[DailyReportItem]:
    reports = db.scalars(select(DailyReport).order_by(DailyReport.report_date.desc()).limit(limit)).all()
    return [_daily_report_for_response(report, settings) for report in reports]


@router.get("/api/sources/status")
def source_status(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[dict]:
    sources = db.scalars(select(Source).order_by(Source.credibility.desc(), Source.name)).all()
    return [
        {
            "id": source.id,
            "name": source.name,
            "kind": source.kind,
            "enabled": source.enabled,
            "last_checked_at": to_local_datetime(source.last_checked_at, settings.timezone),
            "last_success_at": to_local_datetime(source.last_success_at, settings.timezone),
            "last_error": source.last_error,
        }
        for source in sources
    ]


@router.post("/api/intake/url", dependencies=[Depends(require_admin_token)])
async def intake_url(
    payload: ManualUrlRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    """抓取并分析人工发现的高价值链接，不允许客户端直接提交正文。"""

    source = db.scalar(select(Source).where(Source.kind == "manual"))
    if not source:
        raise HTTPException(status_code=503, detail="人工补录来源未初始化")
    try:
        async with httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            headers={"User-Agent": settings.user_agent},
        ) as client:
            item = await fetch_manual_item(str(payload.url), client, override_title=payload.title)
        pipeline = IntelligencePipeline(settings, FeishuNotifier(settings))
        event = await pipeline.process_item(db, source, item)
    except UnsafeUrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"页面抓取失败: {exc}") from exc
    return {"accepted": True, "event_id": event.id if event else None, "message": "内容已处理"}


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    alert_level: str | None = None,
    event_type: str | None = None,
    direction: str | None = None,
    db: Session = Depends(get_db),
):
    events = db.scalars(_event_query(
        alert_level=alert_level, event_type=event_type, direction=direction, start_at=None
    ).limit(100)).all()
    sources = db.scalars(select(Source).where(Source.enabled.is_(True)).order_by(Source.name)).all()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "events": events,
            "sources": sources,
            "filters": {"alert_level": alert_level or "", "event_type": event_type or "", "direction": direction or ""},
        },
    )


@router.get("/events/{event_id}", response_class=HTMLResponse)
def event_page(request: Request, event_id: int, db: Session = Depends(get_db)):
    event = db.scalar(
        select(Event)
        .options(selectinload(Event.evidence).selectinload(EventEvidence.raw_item))
        .where(Event.id == event_id)
    )
    if not event:
        raise HTTPException(status_code=404, detail="事件不存在")
    return templates.TemplateResponse(request=request, name="detail.html", context={"event": event})
