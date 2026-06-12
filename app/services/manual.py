import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from app.models import utc_now
from app.schemas import CollectedItem
from app.services.text import compact_whitespace


class UnsafeUrlError(ValueError):
    """人工 URL 指向内网、环回或非 HTTP 资源时抛出，防止接口成为 SSRF 跳板。"""


def validate_public_url(url: str) -> None:
    """校验 URL 及其 DNS 解析结果均为公网地址。

    仅检查字符串不能拦截解析到 127.0.0.1 的域名，因此这里同时检查全部 A/AAAA 记录；
    重定向后的目标也会再次执行相同校验。
    """

    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise UnsafeUrlError("仅允许带主机名的 HTTP/HTTPS URL")
    if parts.username or parts.password:
        raise UnsafeUrlError("URL 不允许包含用户名或密码")
    try:
        default_port = 443 if parts.scheme == "https" else 80
        addresses = {info[4][0] for info in socket.getaddrinfo(parts.hostname, parts.port or default_port)}
    except socket.gaierror as exc:
        raise UnsafeUrlError("URL 主机名无法解析") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise UnsafeUrlError("不允许访问内网、环回、链路本地或保留地址")


async def fetch_manual_item(
    url: str,
    client: httpx.AsyncClient,
    *,
    override_title: str | None = None,
) -> CollectedItem:
    """抓取人工提交页面，并限制重定向次数和正文大小。"""

    current_url = url
    response: httpx.Response | None = None
    for _ in range(4):
        validate_public_url(current_url)
        response = await client.get(current_url, follow_redirects=False)
        if response.is_redirect:
            location = response.headers.get("location")
            if not location:
                raise httpx.HTTPError("重定向响应缺少 Location")
            current_url = urljoin(current_url, location)
            continue
        response.raise_for_status()
        break
    else:
        raise httpx.TooManyRedirects("人工 URL 重定向超过 3 次")

    assert response is not None
    content_length = int(response.headers.get("content-length", 0) or 0)
    if content_length > 2_000_000 or len(response.content) > 2_000_000:
        raise ValueError("页面超过 2 MB，拒绝进入文本分析")
    content_type = response.headers.get("content-type", "")
    if "html" not in content_type and "text" not in content_type:
        raise ValueError("人工补录仅支持 HTML 或纯文本页面")

    soup = BeautifulSoup(response.text, "html.parser")
    for node in soup.select("script, style, nav, footer, header, noscript, svg"):
        node.decompose()
    content_node = soup.select_one("article, main, [role='main']") or soup.body or soup
    body = compact_whitespace(content_node.get_text(" ", strip=True))
    if len(body) < 50:
        raise ValueError("页面正文过短，无法形成可靠证据")
    page_title = soup.title.string if soup.title and soup.title.string else current_url
    title = compact_whitespace(override_title or page_title)
    return CollectedItem(
        external_id=current_url,
        url=current_url,
        title=title,
        body=body,
        published_at=utc_now(),
        metadata={"platform": "manual", "submitted_url": url},
    )
