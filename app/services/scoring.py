import math

from app.models import RawItem, Source
from app.schemas import MarketAnalysis
from app.services.relevance import contains_hard_trigger


RUMOR_WORDS = ("rumor", "leak", "unconfirmed", "传闻", "爆料", "未证实", "据说")
OFFICIAL_STRUCTURAL_TAGS = {
    "new_case",
    "new_capsule",
    "new_collection",
    "weekly_drop",
    "major_sticker_sale",
    "souvenir_upgrade",
    "souvenir_drop",
    "trade_limit",
    "market_restriction",
}
LOW_URGENCY_TAGS = {"major_shop_pricing", "music_kit_release", "map_pool_change"}
LOW_SIGNAL_ONLY_TAGS = {"music_kit_release"}
SIGNAL_WEIGHTS = {
    "new_case": 34,
    "new_capsule": 32,
    "new_collection": 30,
    "new_gloves": 18,
    "weekly_drop": 26,
    "viewer_pass": 20,
    "major_sticker_sale": 30,
    "major_shop_pricing": 16,
    "souvenir_upgrade": 28,
    "souvenir_drop": 24,
    "trade_limit": 30,
    "market_restriction": 36,
    "inventory_rule": 18,
    "container_opening_rule": 22,
    "map_pool_change": 14,
    "weapon_balance": 10,
    "player_hype": 12,
    "team_hype": 10,
    "content_creator_hype": 8,
    "music_kit_release": 10,
}
SCOPE_SCORES = {
    "个别饰品": 4,
    "单一品类": 8,
    "多品类": 11,
    "全市场机制": 14,
}


def _signal_score(tags: list[str]) -> float:
    """把离散标签压缩成稳定分值。

    只取前几项并逐步衰减，是为了避免同一条官方公告因为模型列出很多相近标签而被重复加分。
    """

    weights = sorted((SIGNAL_WEIGHTS.get(tag, 0) for tag in dict.fromkeys(tags)), reverse=True)
    if not weights:
        return 0.0
    score = 0.0
    factors = (1.0, 0.45, 0.2)
    for factor, weight in zip(factors, weights):
        score += weight * factor
    return min(36.0, score)


def calculate_importance(
    source: Source,
    item: RawItem,
    analysis: MarketAnalysis,
    *,
    evidence_count: int = 1,
    is_new_event: bool = True,
) -> int:
    """计算 0 到 100 的事件重要性。

    新评分更偏向“市场结构变化”而不是“标题看起来像大更新”。这样官方公告里真正会改变
    供给、流动性、赛事商店机制的内容会被抬高，而普通修 bug 的 Counter-Strike 2 Update
    不会再因为来源可信度高就天然拿到高评级。
    """

    official = source.kind in {"steam_news", "monitored_page"} and source.credibility >= 90
    source_score = 22.0 if official else min(18.0, max(0.0, source.credibility * 0.18))
    impact_score = analysis.impact_strength * 4.0
    scope_score = float(SCOPE_SCORES.get(analysis.impact_scope, 4))
    signal_score = _signal_score(analysis.market_signal_tags)
    confidence_score = min(8.0, analysis.confidence * 8.0)
    novelty_score = 6.0 if is_new_event else 2.0
    corroboration_score = min(8.0, max(0, evidence_count - 1) * 3.0)

    metadata = item.metadata_json or {}
    engagement = max(0, int(metadata.get("score", 0))) + max(0, int(metadata.get("comments", 0))) * 2
    engagement_score = min(6.0, math.log10(engagement + 1) * 2.0) if engagement else 0.0

    reason_bonus = 4.0 if official and analysis.market_relevance_reason else 0.0
    relevance_penalty = 10.0 if not analysis.is_market_relevant else 0.0

    text = f"{item.title} {item.body}".lower()
    rumor_penalty = 18.0 if any(word in text for word in RUMOR_WORDS) else 0.0
    total = (
        source_score
        + impact_score
        + scope_score
        + signal_score
        + confidence_score
        + novelty_score
        + corroboration_score
        + engagement_score
        + reason_bonus
    )
    return round(max(0.0, min(100.0, total - rumor_penalty - relevance_penalty)))


def determine_alert_level(
    source: Source,
    item: RawItem,
    analysis: MarketAnalysis,
    score: int,
    evidence_count: int,
) -> str:
    """根据市场结构信号决定 P0 到 P3。"""

    official = source.kind in {"steam_news", "monitored_page"} and source.credibility >= 90
    tags = set(analysis.market_signal_tags)
    if official and contains_hard_trigger(item.title, item.body):
        return "P0"
    if official and tags & OFFICIAL_STRUCTURAL_TAGS and analysis.impact_strength >= 3 and analysis.confidence >= 0.6:
        return "P0"
    if tags and tags <= LOW_SIGNAL_ONLY_TAGS:
        return "P3"
    if official and tags and tags <= LOW_URGENCY_TAGS:
        return "P2" if score >= 60 else "P3"
    if official and score >= 75 and analysis.confidence >= 0.55 and tags:
        return "P1"
    if score >= 82 and analysis.confidence >= 0.75:
        return "P1"
    if score >= 72 and evidence_count >= 2 and analysis.confidence >= 0.65:
        return "P1"
    if score >= 60:
        return "P2"
    return "P3"
