import re


MARKET_KEYWORDS = {
    "case", "capsule", "skin", "sticker", "souvenir", "drop", "trade", "market", "inventory", "collection",
    "箱", "武器箱", "胶囊", "饰品", "皮肤", "贴纸", "纪念品", "掉落", "交易", "市场", "库存", "收藏品",
    "major", "iem", "blast", "pgl", "esl", "s1mple", "m0nesy", "zywoo", "donk",
    "ak-47", "awp", "m4a1-s", "m4a4", "deagle", "usp-s", "glock",
}

HARD_TRIGGER_KEYWORDS = {
    "new case", "new capsule", "new collection", "drop pool", "trade restriction", "market restriction",
    "新武器箱", "新箱子", "新胶囊", "新收藏品", "掉落调整", "交易限制", "市场限制", "交易冷却",
}


def is_candidate(title: str, body: str, source_kind: str) -> bool:
    """低成本预筛候选，官方来源放宽条件以降低漏掉隐晦规则变更的风险。"""

    if source_kind in {"steam_news", "monitored_page"}:
        return True
    text = f"{title} {body}".lower()
    return any(keyword in text for keyword in MARKET_KEYWORDS)


def contains_hard_trigger(title: str, body: str) -> bool:
    """识别无需等待互动数据的官方硬触发事件。"""

    text = re.sub(r"\s+", " ", f"{title} {body}".lower())
    return any(keyword in text for keyword in HARD_TRIGGER_KEYWORDS)

