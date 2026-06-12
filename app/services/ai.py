import json
import logging
import re
from typing import Any, Iterable

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.models import RawItem
from app.schemas import (
    AffectedAsset,
    EventMergeCandidate,
    EventMergeDecision,
    MarketAnalysis,
)
from app.services.relevance import MARKET_KEYWORDS

logger = logging.getLogger(__name__)

PROMPT_VERSION = "2026-06-11.v2"
EVENT_TYPES = [str(choice) for choice in MarketAnalysis.model_fields["event_type"].annotation.__args__]
IMPACT_SCOPES = [str(choice) for choice in MarketAnalysis.model_fields["impact_scope"].annotation.__args__]

# 这些标签专门服务于评分和归并锚点，保持数量有限，避免模型输出过于发散。
MARKET_SIGNAL_TAGS = [
    "new_case",
    "new_capsule",
    "new_collection",
    "new_gloves",
    "weekly_drop",
    "viewer_pass",
    "major_sticker_sale",
    "major_shop_pricing",
    "souvenir_upgrade",
    "souvenir_drop",
    "trade_limit",
    "market_restriction",
    "inventory_rule",
    "container_opening_rule",
    "map_pool_change",
    "weapon_balance",
    "player_hype",
    "team_hype",
    "content_creator_hype",
    "music_kit_release",
]

RULE_TAG_KEYWORDS: dict[str, tuple[str, ...]] = {
    "new_case": ("new case", "weapon case", "箱子", "武器箱"),
    "new_capsule": ("new capsule", "capsule", "胶囊"),
    "new_collection": ("new collection", "collection", "收藏品"),
    "new_gloves": ("new gloves", "gloves", "手套"),
    "weekly_drop": ("weekly drop", "care package", "每周掉落", "周常奖励"),
    "viewer_pass": ("viewer pass", "viewer pass", "观赛通行证", "viewer pass"),
    "major_sticker_sale": ("team and player autographs", "autograph", "sticker sale", "贴纸", "签名"),
    "major_shop_pricing": ("lowest and highest sticker price", "dynamic pricing", "价格区间", "动态定价"),
    "souvenir_upgrade": ("souvenir", "trade up", "纪念品", "交易升级"),
    "souvenir_drop": ("souvenir package", "纪念品包"),
    "trade_limit": ("trade offers", "trade offer", "1000 items", "交易报价", "交易限制"),
    "market_restriction": ("market restriction", "市场限制", "市场规则"),
    "inventory_rule": ("inventory is full", "inventory", "库存已满", "库存限制"),
    "container_opening_rule": ("x-ray scanner", "containers can only be opened", "x-ray", "X光扫描仪"),
    "map_pool_change": ("active duty", "map pool", "added cache", "major hub", "地图池", "加入竞技"),
    "weapon_balance": ("damage", "recoil", "reload", "weapon balance", "平衡", "削弱", "增强"),
    "player_hype": ("s1mple", "m0nesy", "zywoo", "donk"),
    "team_hype": ("vitality", "spirit", "navi", "g2", "faze"),
    "content_creator_hype": ("youtube", "bilibili", "reddit", "twitter", "x.com"),
    "music_kit_release": ("music kit", "music kits", "音乐盒", "音乐包"),
}

PLAYERS = ("s1mple", "m0nesy", "zywoo", "donk")
WEAPONS = ("ak-47", "awp", "m4a1-s", "m4a4", "desert eagle", "deagle", "usp-s", "glock")


class AIAnalysisError(RuntimeError):
    """外部模型返回不可验证结构时抛出。"""


def _extract_tags(text: str) -> list[str]:
    """从文本中提取稳定市场标签。

    规则标签不是为了替代模型，而是提供模型失败时的最小可用锚点，并在评分时保证
    “新收藏品/交易限制/赛事商店机制”这类硬信号不会被一条泛化摘要稀释掉。
    """

    lowered = text.lower()
    tags = [tag for tag, keywords in RULE_TAG_KEYWORDS.items() if any(keyword.lower() in lowered for keyword in keywords)]
    return list(dict.fromkeys(tags))


def _event_type_from_tags(tags: set[str]) -> str:
    if tags & {"new_case", "new_capsule", "new_collection", "new_gloves", "music_kit_release"}:
        return "新品供给"
    if tags & {"weekly_drop", "container_opening_rule"}:
        return "掉落与容器机制"
    if tags & {"viewer_pass", "major_sticker_sale", "major_shop_pricing", "souvenir_upgrade", "souvenir_drop"}:
        return "赛事商店与贴纸"
    if tags & {"trade_limit", "market_restriction", "inventory_rule"}:
        return "交易与市场规则"
    if tags & {"map_pool_change"}:
        return "地图收藏品与纪念品"
    if tags & {"weapon_balance"}:
        return "武器与玩法平衡"
    if tags & {"player_hype", "team_hype"}:
        return "职业与赛事热度"
    if tags & {"content_creator_hype"}:
        return "社区热点"
    return "常规更新"


def _impact_scope_from_tags(tags: set[str]) -> str:
    if tags & {"trade_limit", "market_restriction"}:
        return "全市场机制"
    if tags & {"viewer_pass", "major_sticker_sale", "major_shop_pricing", "souvenir_upgrade", "souvenir_drop"}:
        return "多品类"
    if tags & {"new_case", "new_capsule", "new_collection", "new_gloves", "weekly_drop"}:
        return "多品类"
    if tags & {"map_pool_change", "music_kit_release"}:
        return "单一品类"
    if tags & {"weapon_balance", "player_hype", "team_hype", "content_creator_hype"}:
        return "个别饰品"
    return "个别饰品"


def _mechanisms_from_tags(tags: set[str]) -> list[str]:
    mechanisms: list[str] = []
    if tags & {"new_case", "new_capsule", "new_collection", "new_gloves", "weekly_drop", "souvenir_drop"}:
        mechanisms.append("供给")
    if tags & {"trade_limit", "market_restriction", "inventory_rule", "container_opening_rule"}:
        mechanisms.append("流动性")
    if tags & {"major_sticker_sale", "viewer_pass", "major_shop_pricing", "player_hype", "team_hype"}:
        mechanisms.append("需求")
    if tags & {"map_pool_change", "content_creator_hype"}:
        mechanisms.append("关注度")
    if tags & {"souvenir_upgrade", "major_shop_pricing"}:
        mechanisms.append("稀缺性")
    return mechanisms or ["关注度"]


def _merge_keys_from_tags_and_entities(tags: Iterable[str], entities: dict[str, list[str]]) -> list[str]:
    keys: list[str] = list(tags)
    for values in entities.values():
        for value in values:
            normalized = value.strip().lower()
            if normalized and normalized not in keys:
                keys.append(normalized)
    return keys[:6]


class RuleBasedAnalyzer:
    """无模型密钥时的保守降级分析器。"""

    provider = "rule"
    model = "keyword-v2"

    async def analyze(self, item: RawItem) -> tuple[MarketAnalysis, str | None]:
        text = f"{item.title} {item.body}".lower()
        tags = _extract_tags(text)
        relevant = bool(tags) or any(keyword in text for keyword in MARKET_KEYWORDS)
        tag_set = set(tags)
        event_type = _event_type_from_tags(tag_set) if relevant else "常规更新"
        players = [name for name in PLAYERS if name in text]
        weapons = [name.upper() if name == "awp" else name for name in WEAPONS if name in text]

        if tag_set & {"trade_limit", "market_restriction"}:
            direction = "利空"
            impact_strength = 4
            time_horizon = "1-4周"
        elif tag_set & {"new_case", "new_capsule", "new_collection", "new_gloves", "weekly_drop"}:
            direction = "混合"
            impact_strength = 4
            time_horizon = "1-4周"
        elif tag_set & {"viewer_pass", "major_sticker_sale", "souvenir_upgrade", "souvenir_drop"}:
            direction = "混合"
            impact_strength = 4
            time_horizon = "2-7天"
        elif tag_set & {"container_opening_rule", "major_shop_pricing"}:
            direction = "中性"
            impact_strength = 3
            time_horizon = "2-7天"
        elif tag_set & {"map_pool_change", "player_hype", "team_hype", "content_creator_hype"}:
            direction = "利多"
            impact_strength = 2
            time_horizon = "2-7天"
        else:
            direction = "中性"
            impact_strength = 1 if not relevant else 2
            time_horizon = "0-24小时"

        asset_category = {
            "新品供给": "新品及其相关存量饰品",
            "掉落与容器机制": "掉落池与容器相关饰品",
            "赛事商店与贴纸": "贴纸、纪念品与赛事物品",
            "交易与市场规则": "全市场流动性",
            "地图收藏品与纪念品": "地图收藏品与纪念品",
            "武器与玩法平衡": "相关武器皮肤",
            "职业与赛事热度": "选手、战队与常用武器相关饰品",
            "社区热点": "被讨论的具体饰品或品类",
        }.get(event_type, "相关饰品")

        entities: dict[str, list[str]] = {"players": players, "weapons": weapons}
        analysis = MarketAnalysis(
            is_market_relevant=relevant,
            event_type=event_type,
            summary_zh=item.title if len(item.title) <= 180 else item.title[:177] + "...",
            verified_facts=[item.title],
            entities=entities,
            affected_assets=[
                AffectedAsset(
                    category=asset_category,
                    names=weapons + players,
                    direction=direction,
                    reason="由规则标签和实体直接关联推断，供降级模式使用。",
                )
            ]
            if relevant
            else [],
            direction=direction,
            impact_strength=impact_strength,
            time_horizon=time_horizon,
            market_mechanisms=_mechanisms_from_tags(tag_set),
            confidence=0.65 if relevant else 0.4,
            impact_scope=_impact_scope_from_tags(tag_set) if relevant else "个别饰品",
            market_relevance_reason="降级模式基于标题、正文中的市场信号标签推断。",
            market_signal_tags=tags,
            merge_keys=_merge_keys_from_tags_and_entities(tags, entities),
            uncertainties=["当前为规则降级分析，建议在 AI 可用时重跑以提升边界判断精度。"],
            evidence_refs=[item.canonical_url],
        )
        return analysis, None


class NullEventMerger:
    """AI 不可用时的保守归并器。

    回退策略明确选择“默认不合并”，因为误合并会直接污染最终事件接口，而漏合并只是多出几条
    候选事件，后果更可控。
    """

    provider = "none"

    async def compare(
        self,
        candidate: EventMergeCandidate,
        item: RawItem,
        analysis: MarketAnalysis,
    ) -> EventMergeDecision:
        return EventMergeDecision(
            same_event=False,
            confidence=0.0,
            reason="AI 归并不可用时默认不合并，避免把不同官方更新压成同一事件。",
        )


class OpenAICompatibleAnalyzer:
    """调用 OpenAI 兼容 chat/completions 接口并校验结构化分析结果。"""

    provider = "openai-compatible"

    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self.model = settings.ai_model

    async def analyze(self, item: RawItem) -> tuple[MarketAnalysis, str]:
        if not self.settings.ai_api_key:
            raise AIAnalysisError("AI_ENABLED=true 但未配置 AI_API_KEY")

        payload: dict[str, Any] = {
            "model": self.settings.ai_model,
            "temperature": 0.05,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是谨慎的 CS2 饰品市场情报分析员。"
                        "重点识别会改变饰品供给、赛事商店、贴纸/纪念品、交易限制、库存流动性、地图收藏品热度的事实。"
                        "官方更新里只要出现新箱子、新胶囊、新收藏品、每周掉落、Viewer Pass、贴纸商店机制、纪念品机制、交易/市场限制、库存规则、枪械平衡、饰品视觉效果变化等内容，"
                        "通常都应视为市场相关，除非影响明确可以忽略。"
                        "不把普通bugfix误判成高影响。"
                    ),
                },
                {"role": "user", "content": self._build_prompt(item)},
            ],
        }
        last_error: Exception | None = None
        for _ in range(self.settings.ai_max_retries + 1):
            try:
                logger.debug("向 AI 请求市场分析：原始ID=%s，模型=%s", item.id, self.settings.ai_model)
                response = await self.client.post(
                    f"{self.settings.ai_base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {self.settings.ai_api_key}"},
                    json=payload,
                    timeout=self.settings.ai_timeout_seconds,
                )
                response.raise_for_status()
                raw = response.json()["choices"][0]["message"]["content"]
                parsed = MarketAnalysis.model_validate(self._parse_json(raw))
                logger.debug(
                    "AI 市场分析完成：原始ID=%s，市场相关=%s，事件类型=%s，置信度=%.2f，标签=%s",
                    item.id,
                    parsed.is_market_relevant,
                    parsed.event_type,
                    parsed.confidence,
                    ",".join(parsed.market_signal_tags),
                )
                return parsed, raw
            except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError, ValidationError) as exc:
                last_error = exc
                logger.warning("AI 市场分析单次尝试失败：原始ID=%s，错误=%s", item.id, exc)
        raise AIAnalysisError(f"模型输出在重试后仍不可用: {last_error}")

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.IGNORECASE)
        return json.loads(cleaned)

    @staticmethod
    def _build_prompt(item: RawItem) -> str:
        schema = MarketAnalysis.model_json_schema()
        return (
            "请判断下面这条信息是否会影响 CS2 饰品市场。"
            "优先看：新供给、掉落规则、赛事商店/贴纸/纪念品机制、交易和市场流动性限制带来的需求变化。"
            "event_type 只能从以下值中选择："
            f"{EVENT_TYPES}。"
            "impact_scope 只能从以下值中选择："
            f"{IMPACT_SCOPES}。"
            "market_signal_tags 只能使用以下标签中的若干个："
            f"{MARKET_SIGNAL_TAGS}。"
            "merge_keys 需要给出 2 到 6 个足以标识同一事件的具体锚点，例如收藏品名称、赛事名、"
            "地区规则名、贴纸商店机制名，不要只写 'update' 或 'cs2' 这种泛词。\n"
            f"来源 URL: {item.canonical_url}\n"
            f"标题: {item.title}\n"
            f"正文: {item.body[:12000]}\n"
            f"严格按照以下 JSON Schema 输出，不要输出额外文字：{json.dumps(schema, ensure_ascii=False)}"
        )


class OpenAICompatibleEventMerger:
    """使用 AI 判断两个候选是否属于同一市场事件。"""

    provider = "openai-compatible"

    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self.model = settings.ai_model

    async def compare(
        self,
        candidate: EventMergeCandidate,
        item: RawItem,
        analysis: MarketAnalysis,
    ) -> EventMergeDecision:
        if not self.settings.ai_api_key:
            raise AIAnalysisError("AI_ENABLED=true 但未配置 AI_API_KEY")

        payload: dict[str, Any] = {
            "model": self.settings.ai_model,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是严格的事件去重器。"
                        "只有在两个候选描述的是同一个具体市场事件时才允许合并。"
                        "绝不能因为标题都叫 Counter-Strike 2 Update、都属于同一赛事、"
                        "都和同一地图或同一大类主题相关，就判定为同一事件。"
                        "不同发布日期的不同官方更新通常是不同事件；"
                        "只有在它们围绕同一个机制变化或同一个具体物品/商店改动时，才判定 same_event=true。"
                    ),
                },
                {"role": "user", "content": self._build_prompt(candidate, item, analysis)},
            ],
        }
        last_error: Exception | None = None
        for _ in range(self.settings.ai_max_retries + 1):
            try:
                logger.debug(
            "向 AI 请求事件归并判定：原始ID=%s，候选事件ID=%s，模型=%s",
                    item.id,
                    candidate.event_id,
                    self.settings.ai_model,
                )
                response = await self.client.post(
                    f"{self.settings.ai_base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {self.settings.ai_api_key}"},
                    json=payload,
                    timeout=self.settings.ai_timeout_seconds,
                )
                response.raise_for_status()
                raw = response.json()["choices"][0]["message"]["content"]
                parsed = EventMergeDecision.model_validate(OpenAICompatibleAnalyzer._parse_json(raw))
                logger.debug(
            "AI 归并判定完成：原始ID=%s，候选事件ID=%s，是否同一事件=%s，置信度=%.2f",
                    item.id,
                    candidate.event_id,
                    parsed.same_event,
                    parsed.confidence,
                )
                return parsed
            except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError, ValidationError) as exc:
                last_error = exc
                logger.warning(
            "AI 归并判定单次尝试失败：原始ID=%s，候选事件ID=%s，错误=%s",
                    item.id,
                    candidate.event_id,
                    exc,
                )
        raise AIAnalysisError(f"事件归并判定失败: {last_error}")

    @staticmethod
    def _build_prompt(candidate: EventMergeCandidate, item: RawItem, analysis: MarketAnalysis) -> str:
        schema = EventMergeDecision.model_json_schema()
        return (
            "判断下面这个候选事件和新证据是否属于同一个市场事件。\n"
            "候选事件：\n"
            f"{json.dumps(candidate.model_dump(mode='json'), ensure_ascii=False)}\n"
            "新证据：\n"
            f"{json.dumps({'title': item.title, 'published_at': item.published_at.isoformat(), 'analysis': analysis.model_dump(mode='json')}, ensure_ascii=False)}\n"
            "如果只是同一赛事/同类更新/同一地图的大主题，但不是同一个具体机制变化，请返回 same_event=false。\n"
            f"严格按以下 JSON Schema 输出：{json.dumps(schema, ensure_ascii=False)}"
        )
