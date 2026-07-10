import httpx
from asgi_lifespan import LifespanManager

from app.main import app


async def test_健康检查与模型列表():
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}

            resp = await client.get("/api/v1/models")
            assert resp.status_code == 200
            data = resp.json()
            assert data["default_model"] == "deepseek-chat"
            names = [m["name"] for m in data["models"]]
            assert "deepseek-chat" in names
            for m in data["models"]:
                assert "api_key_env" not in m
