import hashlib
import html
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup


TRACKING_QUERY_PREFIXES = ("utm_", "spm", "from", "ref", "feature")


def compact_whitespace(value: str) -> str:
    """压缩空白但保留自然分词边界，防止格式差异影响哈希与相似度。"""

    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def clean_html(value: str) -> str:
    """移除脚本和标签，只保留用于分析的可见文本。"""

    soup = BeautifulSoup(value or "", "html.parser")
    for node in soup.select("script, style, noscript, svg"):
        node.decompose()
    return compact_whitespace(soup.get_text(" ", strip=True))


def canonicalize_url(value: str) -> str:
    """移除常见追踪参数并稳定排序，避免同一文章因分享参数被重复入库。"""

    parts = urlsplit(value.strip())
    query = [
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if not any(key.lower().startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES)
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(sorted(query)), ""))


def normalize_title(value: str) -> str:
    """生成用于比较的标题，不修改最终展示文本。"""

    value = compact_whitespace(value).lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)


def content_hash(title: str, body: str) -> str:
    """正文只取稳定文本计算哈希，精确去重不依赖平台外部 ID。"""

    material = f"{normalize_title(title)}\n{compact_whitespace(body)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def trigram_similarity(left: str, right: str) -> float:
    """计算字符三元组 Jaccard 相似度。

    中英文混合标题不适合只按空格分词；字符三元组能以很低成本识别“同一公告的不同翻译”，
    且在 MVP 数据量下比引入向量数据库更可控。
    """

    def trigrams(text: str) -> set[str]:
        normalized = normalize_title(text)
        if len(normalized) < 3:
            return {normalized} if normalized else set()
        return {normalized[index : index + 3] for index in range(len(normalized) - 2)}

    left_set, right_set = trigrams(left), trigrams(right)
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)

