from datetime import datetime, timezone

import pytest

from app.models import RawItem, Source
from app.services.ai import RuleBasedAnalyzer
from app.services.scoring import calculate_importance, determine_alert_level


@pytest.mark.asyncio
async def test_rule_analyzer_keeps_official_supply_event_operational():
    """验证未配置外部模型时，新箱子/收藏品类官方更新仍能得到稳定结构化输出。"""

    item = RawItem(
        source_id=1,
        external_id="1",
        canonical_url="https://example.com/update",
        title="CS2 Update: New Case and Collection",
        body="A new weapon case entered the weekly drop pool and introduced a new collection.",
        published_at=datetime.now(timezone.utc),
        content_hash="x",
        metadata_json={},
    )
    analysis, raw = await RuleBasedAnalyzer().analyze(item)
    assert raw is None
    assert analysis.is_market_relevant is True
    assert analysis.event_type in {"新品供给", "掉落与容器机制"}
    assert "new_case" in analysis.market_signal_tags
    assert analysis.impact_scope == "多品类"


@pytest.mark.asyncio
async def test_official_structural_change_scores_high_and_becomes_p0():
    """验证官方供给类硬信号不会再被打成中低分。"""

    source = Source(name="官方", kind="steam_news", url="https://example.com", credibility=100, config={})
    item = RawItem(
        source_id=1,
        external_id="1",
        canonical_url="https://example.com/update",
        title="New Collection Released",
        body="A new collection entered the weekly drop pool.",
        published_at=datetime.now(timezone.utc),
        content_hash="x",
        metadata_json={},
    )
    analysis, _ = await RuleBasedAnalyzer().analyze(item)
    score = calculate_importance(source, item, analysis)
    assert score >= 85
    assert determine_alert_level(source, item, analysis, score, 1) == "P0"


@pytest.mark.asyncio
async def test_official_market_ui_tweak_stays_below_structural_change():
    """验证仅提升价格透明度的官方商店 UI 变动不会和新供给/交易规则拿到同等级。"""

    source = Source(name="官方", kind="steam_news", url="https://example.com", credibility=100, config={})
    item = RawItem(
        source_id=1,
        external_id="1",
        canonical_url="https://example.com/update",
        title="Counter-Strike 2 Update",
        body="Added display of lowest and highest sticker price in the last 7 days in the Major Shop.",
        published_at=datetime.now(timezone.utc),
        content_hash="x",
        metadata_json={},
    )
    analysis, _ = await RuleBasedAnalyzer().analyze(item)
    score = calculate_importance(source, item, analysis)
    assert 60 <= score < 85
    assert determine_alert_level(source, item, analysis, score, 1) == "P2"
