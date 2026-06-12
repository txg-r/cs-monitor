import pytest

from app.services.manual import UnsafeUrlError, validate_public_url


@pytest.mark.parametrize("url", ["http://127.0.0.1/admin", "http://[::1]/", "file:///etc/passwd"])
def test_manual_intake_rejects_private_or_non_http_targets(url):
    """验证人工补录不能访问服务所在内网、环回地址或本地文件。"""

    with pytest.raises(UnsafeUrlError):
        validate_public_url(url)

