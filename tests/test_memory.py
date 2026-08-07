"""M4-W3 测试：用户偏好记忆（F-8-2）、项目记忆（F-8-3）、可见可控（F-8-6）、检索注入（F-8-7）。"""

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.agents import run_generation
from app.main import app
from app.memory import MemoryStore
from tests.stubs import ANALYST_REPLY, StubLLM, generator_reply, make_case, review_reply


@pytest.fixture
def store(tmp_path) -> MemoryStore:
    return MemoryStore(tmp_path / "memory.json")


@pytest.fixture
async def client(tmp_path):
    async with LifespanManager(app):
        app.state.memory = MemoryStore(tmp_path / "memory.json")  # 测试隔离，不落仓库 data/
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


# ---- 存储与可见可控（F-8-6）----


def test_store_crud_and_scopes(store):
    user = store.add("用例步骤保持精简", scope="user")
    proj = store.add("模块结构：大厅/对局/结算", scope="project", project="斗地主")
    assert [e.memory_id for e in store.list(scope="user")] == [user.memory_id]
    assert store.list(scope="project", project="斗地主")[0].memory_id == proj.memory_id
    assert store.list(scope="project", project="其他项目") == []

    updated = store.update(user.memory_id, "步骤精简，每条不超过 5 步")  # 纠错
    assert updated.content == "步骤精简，每条不超过 5 步"
    assert store.delete(proj.memory_id) is True
    assert store.delete(proj.memory_id) is False
    assert store.clear() == 1
    assert store.list() == []


def test_store_validations(store):
    with pytest.raises(ValueError):
        store.add("内容", scope="team")
    with pytest.raises(ValueError):
        store.add("项目记忆缺项目名", scope="project")
    with pytest.raises(ValueError):
        store.add("   ")


def test_store_persistence_roundtrip(tmp_path):
    path = tmp_path / "memory.json"
    MemoryStore(path).add("跨会话保留的偏好", scope="user")
    reloaded = MemoryStore(path)
    assert reloaded.list()[0].content == "跨会话保留的偏好"


# ---- 检索注入（F-8-7）----


def test_retrieve_project_first_and_budget(store):
    store.add("用户偏好甲", scope="user")
    store.add("当前项目的模块结构约定", scope="project", project="斗地主")
    store.add("其他项目的约定", scope="project", project="别的项目")

    notes, snapshot = store.retrieve("斗地主对局需求", project="斗地主", budget_chars=600)
    assert "[项目记忆] 当前项目的模块结构约定" in notes
    assert "[用户偏好] 用户偏好甲" in notes
    assert "其他项目的约定" not in notes  # 项目隔离
    assert notes.index("项目记忆") < notes.index("用户偏好")  # 项目记忆优先
    assert {s["scope"] for s in snapshot} == {"project", "user"}
    assert store.list(scope="user")[0].hits == 1  # 注入计数

    # 预算收紧：装不下的记忆被丢弃
    notes_small, snapshot_small = store.retrieve("斗地主对局需求", project="斗地主", budget_chars=20)
    assert notes_small is None or len(notes_small) <= 40
    assert len(snapshot_small) < len(snapshot)


def test_retrieve_skips_non_inject(store):
    store.record_usage("template", "tpl-a")
    store.record_usage("template", "tpl-a")
    store.record_usage("template", "tpl-a")  # 达阈值 → 沉淀为默认偏好（不注入 Prompt）
    assert store.defaults() == {"template_id": "tpl-a"}
    notes, snapshot = store.retrieve("任意需求", budget_chars=600)
    assert notes is None and snapshot == []


# ---- 使用习惯沉淀（F-8-2）----


def test_usage_revision_pattern_becomes_memory(store):
    store.record_usage("revision", "补充兼容性用例")
    assert store.list() == []  # 一次不沉淀
    store.record_usage("revision", "补充兼容性用例。")  # 归一化后视为同一指令
    entries = store.list()
    assert len(entries) == 1 and entries[0].source == "usage" and entries[0].inject
    assert "补充兼容性用例" in entries[0].content
    # 再次出现只更新，不重复建条
    store.record_usage("revision", "补充兼容性用例")
    assert len(store.list()) == 1


def test_usage_model_defaults_follow_top(store):
    for _ in range(3):
        store.record_usage("model", "deepseek-chat")
    assert store.defaults()["model"] == "deepseek-chat"
    assert store.clear() >= 1
    assert store.defaults() == {}  # 清空同步清零计数


# ---- 生成链路注入（F-8-7）----


async def test_memory_injected_into_generator_prompt():
    llm = StubLLM([ANALYST_REPLY, generator_reply(make_case()), review_reply(True)])
    await run_generation("登录需求", llm=llm, memory_notes="[用户偏好] 每条用例步骤不超过 5 步")
    gen_msg = llm.calls[1]["messages"][1]["content"]
    assert "个性化记忆" in gen_msg and "每条用例步骤不超过 5 步" in gen_msg
    # 拆解与评审 Agent 不注入记忆
    assert "个性化记忆" not in llm.calls[0]["messages"][1]["content"]
    assert "个性化记忆" not in llm.calls[2]["messages"][1]["content"]


# ---- API：CRUD 与任务链路留痕 ----


async def test_memory_api_crud(client):
    resp = await client.post(
        "/api/v1/memories", json={"content": "优先级判定从严", "scope": "user"}
    )
    assert resp.status_code == 200
    memory_id = resp.json()["memory_id"]

    resp = await client.post("/api/v1/memories", json={"content": "x", "scope": "project"})
    assert resp.status_code == 400  # 项目记忆必须带项目名

    resp = await client.put(f"/api/v1/memories/{memory_id}", json={"content": "优先级判定标准从严"})
    assert resp.json()["content"] == "优先级判定标准从严"

    data = (await client.get("/api/v1/memories")).json()
    assert len(data["memories"]) == 1

    assert (await client.delete(f"/api/v1/memories/{memory_id}")).status_code == 200
    assert (await client.delete(f"/api/v1/memories/{memory_id}")).status_code == 404
    assert (await client.delete("/api/v1/memories")).json() == {"cleared": 0}


async def test_task_with_project_injects_and_snapshots(client):
    app.state.memory.add("结算金额预期必须写明计算式", scope="user")
    app.state.memory.add("模块结构：大厅/对局/结算", scope="project", project="斗地主")
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(make_case()), review_reply(True)])

    resp = await client.post(
        "/api/v1/tasks",
        files={"files": ("需求.txt", "# 结算规则".encode(), "text/plain")},
        data={"project": "斗地主"},
    )
    assert resp.status_code == 200
    task_id = resp.json()["task_id"]

    gen_msg = app.state.llm.calls[1]["messages"][1]["content"]
    assert "结算金额预期必须写明计算式" in gen_msg and "大厅/对局/结算" in gen_msg

    record = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    assert record["context"]["project"] == "斗地主"
    contents = [m["content"] for m in record["memories"]]  # 引用可见（F-8-6）
    assert "结算金额预期必须写明计算式" in contents and "模块结构：大厅/对局/结算" in contents


async def test_repeated_revision_precipitates_preference(client):
    async def one_task_with_revision():
        app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(make_case()), review_reply(True)])
        resp = await client.post(
            "/api/v1/tasks", files={"files": ("需求.txt", b"# login", "text/plain")}
        )
        task_id = resp.json()["task_id"]
        app.state.llm = StubLLM([generator_reply(make_case()), review_reply(True)])
        await client.post(
            f"/api/v1/tasks/{task_id}/revise", json={"instruction": "补充兼容性用例"}
        )

    await one_task_with_revision()
    assert (await client.get("/api/v1/memories")).json()["memories"] == []
    await one_task_with_revision()  # 跨任务第二次提出同一修订要求 → 沉淀为偏好
    memories = (await client.get("/api/v1/memories")).json()["memories"]
    assert len(memories) == 1 and "补充兼容性用例" in memories[0]["content"]
