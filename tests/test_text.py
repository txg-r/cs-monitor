from app.services.text import canonicalize_url, content_hash, trigram_similarity


def test_canonicalize_url_removes_tracking_and_fragment():
    """验证分享追踪参数不会让同一篇文章形成多个 URL 身份。"""

    actual = canonicalize_url("HTTPS://Example.com/news/?utm_source=x&b=2&a=1#comments")
    assert actual == "https://example.com/news?a=1&b=2"


def test_content_hash_ignores_formatting_noise():
    """验证大小写和多余空白不会绕过正文精确去重。"""

    assert content_hash("New Case", "hello   world") == content_hash("new case", "hello world")


def test_trigram_similarity_handles_translated_prefixes():
    """验证标题带平台前缀时仍能识别为高度相似的同一事件。"""

    score = trigram_similarity("CS2 Update: New Weapon Case Released", "Breaking CS2 Update New Weapon Case Released")
    assert score > 0.55

