import csv

from openpyxl import load_workbook

from app.exporters import export_csv, export_excel
from app.templates import TestCase
from tests.stubs import make_case


def _cases():
    return [
        TestCase.model_validate(make_case()),
        TestCase.model_validate(
            make_case(
                case_id="TC-登录-002",
                priority="P0",
                title="验证密码错误提示",
                steps=[
                    {"action": "输入错误密码", "expected": "提示「账号或密码错误」"},
                    {"action": "连续错误 5 次", "expected": "账号锁定 30 分钟"},
                ],
            )
        ),
    ]


def test_excel导出(tmp_path):
    path = export_excel(_cases(), tmp_path / "用例.xlsx")
    ws = load_workbook(str(path)).active

    assert [c.value for c in ws[1]] == [
        "用例编号", "所属模块", "用例标题", "优先级", "前置条件", "测试步骤", "预期结果", "关键词", "备注",
    ]
    assert ws.max_row == 3
    # 步骤与预期结果编号对应
    assert ws.cell(row=3, column=6).value == "1. 输入错误密码\n2. 连续错误 5 次"
    assert ws.cell(row=3, column=7).value == "1. 提示「账号或密码错误」\n2. 账号锁定 30 分钟"
    # P0 优先级条件着色（红）
    assert ws.cell(row=3, column=4).fill.fgColor.rgb.endswith("F4CCCC")


def test_csv导出_带BOM(tmp_path):
    path = export_csv(_cases(), tmp_path / "用例.csv")

    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM

    with path.open(encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    assert rows[0][0] == "用例编号"
    assert rows[1][0] == "TC-登录-001"
    assert len(rows) == 3


# ---- 导出文件与页面一致：删除 / 修改 / 审核后下载即重新导出；可只导出正式用例 ----


async def test_导出文件随用例改动重新生成_可只导正式用例(tmp_path):
    import httpx
    from asgi_lifespan import LifespanManager

    from app.main import app
    from tests.stubs import ANALYST_REPLY, StubLLM, generator_reply, make_case, review_reply

    def rows_of(content: bytes) -> list[str]:
        f = tmp_path / "dl.xlsx"
        f.write_bytes(content)
        ws = load_workbook(str(f)).active
        return [str(r[0]) for r in ws.iter_rows(min_row=2, values_only=True) if r and r[0]]

    async with LifespanManager(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(make_case(), make_case(case_id="TC-登录-002", title="密码错误"),
                                                                  make_case(case_id="TC-登录-003", title="账号锁定")), review_reply(True)])
            task_id = (await client.post("/api/v1/tasks", data={"text": "登录需求", "project": "P"})).json()["task_id"]
            r = await client.get(f"/api/v1/tasks/{task_id}/files/xlsx")
            assert r.status_code == 200 and len(rows_of(r.content)) == 3
            # 单条审核里删除一条（此前这条路径不会标记重新导出，下载到的是旧文件）
            assert (await client.post(f"/api/v1/tasks/{task_id}/review",
                                      json={"items": [{"case_id": "TC-登录-003", "action": "delete"}]})).status_code == 200
            r = await client.get(f"/api/v1/tasks/{task_id}/files/xlsx")
            assert len(rows_of(r.content)) == 2
            # 没有正式用例时 scope=approved 为 404
            assert (await client.get(f"/api/v1/tasks/{task_id}/files/xlsx?scope=approved")).status_code == 404
            # 人工修改标题（人工定稿即通过）后导出内容同步；只导正式用例时只有这一条
            await client.post(f"/api/v1/tasks/{task_id}/review",
                              json={"items": [{"case_id": "TC-登录-002", "action": "modify", "case": {**make_case(case_id="TC-登录-002", title="密码错误提示文案")}}]})
            r = await client.get(f"/api/v1/tasks/{task_id}/files/csv")
            assert "密码错误提示文案" in r.content.decode("utf-8-sig")
            r = await client.get(f"/api/v1/tasks/{task_id}/files/xlsx?scope=approved")
            assert r.status_code == 200 and rows_of(r.content) == ["TC-登录-002"]
            # 再通过一条：正式用例 2 行，全量仍 2 行
            await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [{"case_id": "TC-登录-001", "action": "accept"}]})
            assert rows_of((await client.get(f"/api/v1/tasks/{task_id}/files/xlsx?scope=approved")).content) == ["TC-登录-001", "TC-登录-002"]
            assert len(rows_of((await client.get(f"/api/v1/tasks/{task_id}/files/xlsx")).content)) == 2
            assert (await client.get(f"/api/v1/tasks/{task_id}/files/exe")).status_code == 404
