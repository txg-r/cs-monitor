from fastapi.testclient import TestClient


def test_application_registers_routes_and_renders_dashboard():
    """验证完整应用能注册全部路由并渲染首页，捕获仅在 FastAPI 启动时出现的响应模型错误。"""

    from app.main import app

    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "CS2 饰品趋势情报" in dashboard.text
        assert client.get("/api/sources/status").status_code == 200
