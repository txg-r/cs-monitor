import base64
import hashlib
import hmac
import logging
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Event, Notification, utc_now

logger = logging.getLogger(__name__)


class FeishuNotifier:
    """飞书机器人通知器，使用数据库发件箱提供幂等和失败重试。"""

    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def _event_markdown(event: Event, is_update: bool) -> str:
        affected = "、".join(asset.get("category", "相关饰品") for asset in event.affected_assets) or "待确认"
        prefix = "事件更新" if is_update else "新情报"
        return (
            f"**{prefix}：{event.title}**\n"
            f"等级：{event.alert_level}｜评分：{event.importance_score}｜置信度：{event.confidence:.0%}\n"
            f"方向：{event.direction}｜强度：{event.impact_strength}/5｜周期：{event.time_horizon}\n"
            f"影响范围：{affected}\n\n{event.summary}"
        )

    def _payload(self, event: Event, is_update: bool) -> dict:
        color = "red" if event.alert_level == "P0" else "orange"
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {"template": color, "title": {"tag": "plain_text", "content": f"CS2 市场情报 {event.alert_level}"}},
                "elements": [{"tag": "markdown", "content": self._event_markdown(event, is_update)}],
            },
        }
        return self._sign(payload)

    def _sign(self, payload: dict) -> dict:
        """在配置飞书签名密钥时添加时间戳和签名，降低 Webhook 泄露后的滥用风险。"""

        if not self.settings.feishu_secret:
            return payload
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{self.settings.feishu_secret}"
        digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        return {**payload, "timestamp": timestamp, "sign": base64.b64encode(digest).decode("utf-8")}

    async def notify_event(self, db: Session, event: Event, *, is_update: bool = False) -> bool:
        """创建并立即尝试发送事件通知；重复调用由 dedup_key 安全忽略。"""

        suffix = f"update:{event.direction}:{event.impact_strength}" if is_update else "initial"
        dedup_key = f"event:{event.id}:{suffix}"
        notification = Notification(
            event_id=event.id,
            kind="event_update" if is_update else "event_alert",
            dedup_key=dedup_key,
            status="pending" if self.settings.feishu_webhook_url else "disabled",
            payload=self._payload(event, is_update),
        )
        db.add(notification)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            logger.info("通知被幂等去重：幂等键=%s", dedup_key)
            return False
        if not self.settings.feishu_webhook_url:
            logger.info("通知未发送：未配置飞书 Webhook，事件ID=%s，类型=%s", event.id, notification.kind)
            return False
        logger.info("开始发送通知：事件ID=%s，类型=%s，幂等键=%s", event.id, notification.kind, dedup_key)
        return await self._send(db, notification)

    async def _send(self, db: Session, notification: Notification) -> bool:
        notification.attempts += 1
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(self.settings.feishu_webhook_url, json=notification.payload)
                response.raise_for_status()
                result = response.json()
                if result.get("code", result.get("StatusCode", 0)) != 0:
                    raise RuntimeError(f"飞书拒绝消息: {result}")
            notification.status = "sent"
            notification.sent_at = utc_now()
            notification.last_error = None
            if notification.event:
                notification.event.notified_at = notification.sent_at
            db.commit()
            logger.info(
                "通知发送成功：通知ID=%s，类型=%s，尝试次数=%s",
                notification.id,
                notification.kind,
                notification.attempts,
            )
            return True
        except Exception as exc:
            notification.status = "failed" if notification.attempts >= 3 else "pending"
            notification.last_error = str(exc)[:2000]
            db.commit()
            logger.warning(
                "通知发送失败：通知ID=%s，类型=%s，尝试次数=%s，状态=%s，错误=%s",
                notification.id,
                notification.kind,
                notification.attempts,
                notification.status,
                exc,
            )
            return False

    async def retry_pending(self, db: Session) -> int:
        """重试未超过三次的通知，避免短暂网络故障导致紧急事件永久丢失。"""

        if not self.settings.feishu_webhook_url:
            return 0
        pending = db.scalars(
            select(Notification).where(Notification.status == "pending", Notification.attempts < 3).limit(20)
        ).all()
        logger.debug("开始重试待发送通知：数量=%s", len(pending))
        sent = 0
        for notification in pending:
            sent += int(await self._send(db, notification))
        return sent

    async def send_startup_check(self, db: Session) -> bool:
        """应用启动时发送飞书通道自检消息。

        启动自检的语义是“这一次进程启动后的通知通道是否可用”，所以每次启动都应该发送。
        这里仍然写入发件箱表，是为了保留审计记录和失败原因；但 dedup_key 使用随机后缀，避免复用事件通知的幂等策略。
        """

        local_now = utc_now().astimezone(ZoneInfo(self.settings.timezone))
        dedup_key = f"startup-check:{local_now.strftime('%Y%m%dT%H%M%S')}:{uuid4().hex}"
        payload = self._sign(
            {
                "msg_type": "text",
                "content": {
                    "text": (
                        "CS2 情报系统启动自检\n"
                        f"时间：{local_now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                        "结果：飞书推送通道可用。"
                    )
                },
            }
        )
        notification = Notification(
            kind="startup_check",
            dedup_key=dedup_key,
            status="pending" if self.settings.feishu_webhook_url else "disabled",
            payload=payload,
        )
        db.add(notification)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            logger.info("启动自检通知被幂等去重：幂等键=%s", dedup_key)
            return False
        if not self.settings.feishu_webhook_url:
            logger.info("启动自检未发送：未配置飞书 Webhook")
            return False
        logger.info("开始发送启动自检通知：幂等键=%s", dedup_key)
        return await self._send(db, notification)

    async def send_report(self, db: Session, report_id: int, markdown: str) -> bool:
        """发送日报并复用发件箱幂等机制。"""

        dedup_key = f"daily-report:{report_id}"
        payload = self._sign({"msg_type": "text", "content": {"text": markdown}})
        notification = Notification(
            kind="daily_report",
            dedup_key=dedup_key,
            status="pending" if self.settings.feishu_webhook_url else "disabled",
            payload=payload,
        )
        db.add(notification)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            logger.info("日报通知被幂等去重：日报ID=%s", report_id)
            return False
        if not self.settings.feishu_webhook_url:
            logger.info("日报未发送：未配置飞书 Webhook，日报ID=%s", report_id)
            return False
        logger.info("开始发送日报：日报ID=%s", report_id)
        return await self._send(db, notification)
