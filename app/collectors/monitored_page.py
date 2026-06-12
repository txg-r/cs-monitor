import asyncio
import difflib
import hashlib
import json
import re
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup, Tag

from app.collectors.base import BaseCollector, CollectorError
from app.models import Source, utc_now
from app.schemas import CollectedItem, PollResult
from app.services.text import compact_whitespace


class MonitoredPageCollector(BaseCollector):
    """监控低频但高影响的规则页面正文变化。

    这一类页面往往没有稳定 RSS，也不值得做复杂全站抓取，所以首版采用“正文指纹 + 差异片段”模型。
    但 Steam Support FAQ 已经切成 React 壳页，正文藏在隐藏配置里，因此这里要同时支持：
    1. 普通静态正文提取
    2. Steam Support FAQ 的隐藏数据提取
    """

    MIN_BODY_LENGTH = 80
    MAX_RETRIES = 2
    RETRY_BACKOFF_SECONDS = 1.0
    DEFAULT_ACCEPT_LANGUAGE = "en-US,en;q=0.9"
    GENERIC_SELECTORS = (
        "main",
        "article",
        "[role='main']",
        ".faq_content",
        ".faq_answer",
        "#responsive_page_template_content",
    )
    REMOVABLE_SELECTORS = "script, style, nav, footer, header, noscript, svg"

    async def collect(self, source: Source, client: httpx.AsyncClient) -> PollResult:
        response = await self._request_with_retry(source, client)
        soup = BeautifulSoup(response.text, "html.parser")

        title = compact_whitespace(soup.title.string if soup.title and soup.title.string else source.name)
        body = self._extract_body(source, response, soup)
        if len(body) < self.MIN_BODY_LENGTH:
            raise CollectorError("提取正文过短，可能被登录页、壳页面或反爬页面替代")

        fingerprint = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if source.content_fingerprint is None or source.content_snapshot is None:
            # 首次运行只建立基线，不直接产出事件。
            # 否则系统会把规则页多年累积内容误判成“当前刚发生的变化”。
            return PollResult(source_fingerprint=fingerprint, source_snapshot=body, baseline_only=True)
        if fingerprint == source.content_fingerprint:
            return PollResult(source_fingerprint=fingerprint, source_snapshot=body)

        previous_sentences = self._sentences(source.content_snapshot)
        current_sentences = self._sentences(body)
        changes = [
            line
            for line in difflib.ndiff(previous_sentences, current_sentences)
            if line.startswith(("+ ", "- "))
        ]
        change_text = compact_whitespace(" ".join(changes))[:12000]
        if not change_text:
            change_text = body[:12000]

        item = CollectedItem(
            external_id=f"{source.id}:{fingerprint}",
            # 这里使用最终落地 URL，而不是初始 source.url。
            # 原因是 Steam 社区市场 FAQ 会 302 到 help.steampowered.com 的真实 FAQ 地址，
            # 保留最终 URL 更利于后续审计和人工点开查看。
            url=str(response.url),
            title=f"{title}发生变更",
            # 只分析变更片段，避免页面里长期存在的旧规则词汇反复触发高优先级提醒。
            body=change_text,
            published_at=utc_now(),
            metadata={
                "previous_fingerprint": source.content_fingerprint,
                "new_fingerprint": fingerprint,
                "source_url": source.url,
                "final_url": str(response.url),
            },
        )
        return PollResult(items=[item], source_fingerprint=fingerprint, source_snapshot=body)

    async def _request_with_retry(self, source: Source, client: httpx.AsyncClient) -> httpx.Response:
        """请求页面并对 Steam 这类偶发 TLS EOF 做轻量重试。

        这里不做无限重试，因为规则页采集本身不是高频交易链路。
        只要覆盖掉瞬时网络抖动即可，避免把短暂失败直接展示成来源异常。
        """

        headers = {"Accept-Language": source.config.get("accept_language", self.DEFAULT_ACCEPT_LANGUAGE)}
        last_error: Exception | None = None
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                response = await client.get(source.url, headers=headers)
                response.raise_for_status()
                return response
            except (
                httpx.ConnectError,
                httpx.ReadError,
                httpx.ReadTimeout,
                httpx.ConnectTimeout,
                httpx.RemoteProtocolError,
            ) as exc:
                last_error = exc
                if attempt >= self.MAX_RETRIES:
                    break
                await asyncio.sleep(self.RETRY_BACKOFF_SECONDS * (attempt + 1))
            except httpx.HTTPError as exc:
                raise CollectorError(f"页面请求失败：{self._describe_error(exc)}") from exc

        assert last_error is not None
        raise CollectorError(f"页面请求失败：{self._describe_error(last_error)}") from last_error

    def _extract_body(self, source: Source, response: httpx.Response, soup: BeautifulSoup) -> str:
        """从页面中提取可用于指纹和差异比较的正文。

        提取顺序很重要：
        1. 先尊重来源配置里的显式 selector
        2. 再走站点级特化逻辑
        3. 最后才退回通用正文提取
        这样既兼容人工补充来源，也能处理 Steam Support 这类动态壳页。
        """

        selector = source.config.get("content_selector")
        if selector:
            body = self._extract_by_selector(soup, selector)
            if len(body) >= self.MIN_BODY_LENGTH:
                return body

        steam_faq_body = self._extract_steam_support_faq(source, response, soup)
        if len(steam_faq_body) >= self.MIN_BODY_LENGTH:
            return steam_faq_body

        for selector in self.GENERIC_SELECTORS:
            body = self._extract_by_selector(soup, selector)
            if len(body) >= self.MIN_BODY_LENGTH:
                return body

        if soup.body is None:
            raise CollectorError("页面中没有可提取的正文节点")

        fallback_soup = BeautifulSoup(str(soup.body), "html.parser")
        for node in fallback_soup.select(self.REMOVABLE_SELECTORS):
            node.decompose()
        return compact_whitespace(fallback_soup.get_text(" ", strip=True))

    def _extract_by_selector(self, soup: BeautifulSoup, selector: str) -> str:
        node = soup.select_one(selector)
        if not node:
            return ""
        cleaned = BeautifulSoup(str(node), "html.parser")
        for removable in cleaned.select(self.REMOVABLE_SELECTORS):
            removable.decompose()
        return compact_whitespace(cleaned.get_text(" ", strip=True))

    def _extract_steam_support_faq(self, source: Source, response: httpx.Response, soup: BeautifulSoup) -> str:
        """提取 Steam Support React FAQ 隐藏在 `data-faqstore` 里的正文。

        Steam Support 静态 HTML 中的正文容器是空的，真正内容塞在隐藏属性中。
        如果不读这里，采集器只能看到页面标题和导航，自然会误判成“正文过短”。
        """

        host = urlsplit(str(response.url)).netloc.lower()
        if host != "help.steampowered.com":
            return ""

        config_node = soup.select_one("#application_config[data-faqstore]")
        if not isinstance(config_node, Tag):
            return ""
        raw_store = config_node.get("data-faqstore")
        if not raw_store:
            return ""

        try:
            faq_store = json.loads(raw_store)
        except json.JSONDecodeError:
            return ""

        faqs = faq_store.get("faqs")
        if not isinstance(faqs, dict) or not faqs:
            return ""

        target_code = self._faq_code_from_url(str(response.url)) or self._faq_code_from_url(source.url)
        selected: dict | None = None
        if target_code:
            selected = next(
                (
                    faq
                    for faq in faqs.values()
                    if isinstance(faq, dict) and str(faq.get("url_code", "")).upper() == target_code
                ),
                None,
            )
        if selected is None:
            selected = next((faq for faq in faqs.values() if isinstance(faq, dict)), None)
        if selected is None:
            return ""

        title = compact_whitespace(str(selected.get("title", title if (title := source.name) else source.name)))
        body = self._steam_bbcode_to_text(str(selected.get("content", "")))
        if not body:
            return ""
        return compact_whitespace(f"{title}. {body}")

    @staticmethod
    def _faq_code_from_url(url: str) -> str | None:
        match = re.search(r"/faqs/view/([A-Z0-9-]+)", url, flags=re.IGNORECASE)
        return match.group(1).upper() if match else None

    @staticmethod
    def _steam_bbcode_to_text(value: str) -> str:
        """把 Steam FAQ 的 BBCode 风格内容转成稳定纯文本。

        这里不追求完整富文本渲染，只关心“规则语义”是否稳定可比对。
        因此保留标题、列表项和链接文本，去掉样式与锚点即可。
        """

        text = value.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\[url(?:=[^\]]+)?\](.*?)\[/url\]", r"\1", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"\[url [^\]]*\]\s*\[/url\]", " ", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"\[\*\]", "\n- ", text)
        text = re.sub(r"\[/?(?:h[1-4]|b|i|u|list)\]", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"\[section(?: [^\]]*)?\]", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"\[/section\]", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"\[[a-z]+(?:=[^\]]+| [^\]]+)?\]", " ", text, flags=re.IGNORECASE)
        return compact_whitespace(text)

    @staticmethod
    def _describe_error(exc: Exception) -> str:
        message = compact_whitespace(str(exc))
        if message:
            return message
        return f"{exc.__class__.__name__}（未返回明确错误信息）"

    @staticmethod
    def _sentences(value: str) -> list[str]:
        """按中英文句末符号切分页面，保证差异比较尽量落在语义边界上。"""

        return [part.strip() for part in re.split(r"(?<=[。！？?!])\s*", value) if part.strip()]
