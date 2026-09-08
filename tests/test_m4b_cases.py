"""M4b：人工用例集、人工新增/复制（草稿 → 提交评审）、Excel 导入五步校验、批量维护与乐观锁。"""

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.main import app
from tests.stubs import make_case


@pytest.fixture
async def client():
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def _manual_task(client) -> str:
    await client.post("/api/v1/projects", json={"name": "P"})
    resp = await client.post("/api/v1/projects/P/manual-task", json={"title": "登录模块手工用例"})
    assert resp.status_code == 200, resp.text
    return resp.json()["task_id"]


async def test_人工新增_复制_草稿提交评审(client):
    tid = await _manual_task(client)
    body = {"module": "登录", "title": "验证码过期", "priority": "P1", "steps": [{"action": "等待 5 分钟后提交", "expected": "提示验证码已过期"}], "keywords": "登录、验证码"}
    r = (await client.post(f"/api/v1/tasks/{tid}/cases", json=body)).json()
    c = r["case"]
    assert c["case_id"] == "TC-登录-001" and c["source"] == "manual" and c["version"] == 1
    task = (await client.get(f"/api/v1/tasks/{tid}")).json()
    assert task["case_reviews"][c["uid"]]["status"] == "draft"
    # 草稿不能直接通过；提交评审后可通过
    resp = await client.post(f"/api/v1/tasks/{tid}/review", json={"items": [{"case_id": c["case_id"], "action": "approve"}]})
    assert resp.status_code == 409
    await client.post(f"/api/v1/tasks/{tid}/review", json={"items": [{"case_id": c["case_id"], "action": "submit"}]})
    task = (await client.get(f"/api/v1/tasks/{tid}")).json()
    assert task["case_reviews"][c["uid"]]["status"] == "pending"
    assert (await client.post(f"/api/v1/tasks/{tid}/review", json={"items": [{"case_id": c["case_id"], "action": "approve"}]})).status_code == 200
    # 复制：草稿、来源 copy、标题带副本、版本历史首版
    r = (await client.post(f"/api/v1/tasks/{tid}/cases/{c['uid']}/copy")).json()
    dup = r["case"]
    assert dup["title"].endswith("（副本）") and dup["source"] == "copy" and dup["case_id"] == "TC-登录-002"
    task = (await client.get(f"/api/v1/tasks/{tid}")).json()
    assert task["case_reviews"][dup["uid"]]["status"] == "draft"
    vers = (await client.get(f"/api/v1/tasks/{tid}/versions", params={"kind": "case", "entity_id": dup["uid"]})).json()
    assert vers["versions"][0]["source"] == "manual"
    # 非法用例被拒
    assert (await client.post(f"/api/v1/tasks/{tid}/cases", json={**body, "title": ""})).status_code == 400
    # 项目用例库生命周期含草稿
    lib = (await client.get("/api/v1/cases", params={"project": "P"})).json()
    assert {x["review"] for x in lib["cases"]} == {"approved", "draft"}


async def test_Excel导入五步校验(client, tmp_path):
    tid = await _manual_task(client)
    await client.post(f"/api/v1/tasks/{tid}/cases", json={"module": "登录", "title": "已存在的用例", "steps": [{"action": "a", "expected": "b"}]})
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    ws.append(["用例编号", "模块", "用例标题", "优先级", "前置条件", "操作步骤", "预期结果", "关键词", "备注"])
    ws.append(["TC-登录-001", "登录", "正常登录", "P1", "", "1. 输入正确账号", "1. 登录成功", "登录", ""])
    ws.append(["TC-登录-002", "登录", "已存在的用例", "P1", "", "1. x", "1. y", "", ""])
    ws.append(["TC-登录-003", "登录", "正常登录", "P2", "", "1. 重复标题", "1. z", "", ""])
    ws.append(["TC-登录-004", "", "缺模块缺预期", "P3", "", "1. 步骤", "", "", ""])
    ws.append(["TC-登录-005", "", "缺模块", "P3", "", "1. 步骤", "1. 结果", "", ""])
    path = tmp_path / "导入.xlsx"; wb.save(path)
    with open(path, "rb") as fh:
        resp = await client.post(f"/api/v1/tasks/{tid}/cases/import/preview", files={"file": ("导入.xlsx", fh.read(), "application/octet-stream")})
    assert resp.status_code == 200, resp.text
    pv = resp.json()
    assert pv["total"] == 5 and pv["errors"] == 3
    rows = {r["row"]: r for r in pv["rows"]}
    assert rows[1]["errors"] == []
    assert "与任务内已有用例标题重复" in rows[2]["errors"]
    assert any("与第 1 行标题重复" in e for e in rows[3]["errors"])
    assert any("缺少预期结果" in e for e in rows[4]["errors"])
    assert rows[5]["errors"] == [] and rows[5]["case"]["module"] == "未分组" and "缺少模块" in " ".join(rows[5]["warnings"])
    # 有错误行：不显式选择只导有效行则拒绝
    resp = await client.post(f"/api/v1/tasks/{tid}/cases/import/confirm", json={"token": pv["token"]})
    assert resp.status_code == 400 and "3 行" in resp.json()["detail"]
    resp = await client.post(f"/api/v1/tasks/{tid}/cases/import/confirm", json={"token": pv["token"], "only_valid": True})
    assert resp.status_code == 200 and resp.json()["imported"] == 2 and resp.json()["skipped"] == 3
    task = (await client.get(f"/api/v1/tasks/{tid}")).json()
    imported = [c for c in task["result"]["cases"] if c["source"] == "import"]
    assert len(imported) == 2 and all(task["case_reviews"][c["uid"]]["status"] == "draft" for c in imported)
    # 令牌一次性
    assert (await client.post(f"/api/v1/tasks/{tid}/cases/import/confirm", json={"token": pv["token"], "only_valid": True})).status_code == 404
    # 模板下载
    resp = await client.get("/api/v1/cases/import-template")
    assert resp.status_code == 200 and resp.content[:2] == b"PK"


async def test_批量维护_乐观锁_回收站(client):
    tid = await _manual_task(client)
    uids = []
    for i in range(3):
        r = (await client.post(f"/api/v1/tasks/{tid}/cases", json={"module": "登录", "title": f"用例{i}", "steps": [{"action": "a", "expected": "b"}]})).json()
        uids.append(r["case"]["uid"])
    # 批量提交评审 → 全部 pending
    r = (await client.post(f"/api/v1/tasks/{tid}/cases/batch", json={"uids": uids, "action": "submit"})).json()
    assert len(r["applied"]) == 3 and all(v["status"] == "pending" for v in r["case_reviews"].values())
    # 批量改优先级：带页面版本，其中一条版本过期 → 冲突跳过
    r = (await client.post(f"/api/v1/tasks/{tid}/cases/batch",
                           json={"uids": uids, "action": "set_priority", "value": "p0", "base_versions": {uids[0]: 1, uids[1]: 99}})).json()
    assert [c["uid"] for c in r["conflicts"]] == [uids[1]] and len(r["applied"]) == 2
    by = {c["uid"]: c for c in r["cases"]}
    assert by[uids[0]]["priority"] == "P0" and by[uids[0]]["version"] == 2 and by[uids[1]]["priority"] == "P2"
    # 批量加标签（追加去重）与改模块
    r = (await client.post(f"/api/v1/tasks/{tid}/cases/batch", json={"uids": uids[:1], "action": "add_keywords", "value": "回归, 冒烟"})).json()
    assert r["cases"][0]["keywords"] == "回归、冒烟"
    r = (await client.post(f"/api/v1/tasks/{tid}/cases/batch", json={"uids": uids, "action": "set_module", "value": "账号"})).json()
    assert all(c["module"] == "账号" and c["case_id"].startswith("TC-账号-") for c in r["cases"])
    # 已通过锁定的用例跳过
    await client.post(f"/api/v1/tasks/{tid}/review", json={"items": [{"case_id": r["cases"][0]["case_id"], "action": "approve"}]})
    r = (await client.post(f"/api/v1/tasks/{tid}/cases/batch", json={"uids": uids[:1], "action": "set_priority", "value": "P3"})).json()
    assert r["skipped"][0]["reason"].startswith("已通过并锁定")
    # 批量删除 → 回收站可恢复
    r = (await client.post(f"/api/v1/tasks/{tid}/cases/batch", json={"uids": uids[1:], "action": "delete"})).json()
    assert len(r["applied"]) == 2 and len(r["cases"]) == 1
    bin_items = (await client.get(f"/api/v1/tasks/{tid}/recycle-bin")).json()["items"]
    assert len(bin_items) == 2
    assert (await client.post(f"/api/v1/tasks/{tid}/recycle-bin/restore", json={"item_id": bin_items[0]["id"]})).status_code == 200
    assert (await client.post(f"/api/v1/tasks/{tid}/cases/batch", json={"uids": uids, "action": "explode"})).status_code == 400
