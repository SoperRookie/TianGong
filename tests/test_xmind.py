"""XMind 导出测试：结构对齐团队模板约定。"""

import json
import zipfile

from app.exporters import export_xmind
from app.templates import TestCase
from tests.stubs import make_case


def _load_content(path):
    with zipfile.ZipFile(path) as zf:
        assert set(zf.namelist()) >= {"content.json", "metadata.json", "manifest.json"}
        return json.loads(zf.read("content.json"))


def test_xmind结构与团队模板一致(tmp_path):
    cases = [
        TestCase.model_validate(make_case()),
        TestCase.model_validate(
            make_case(
                case_id="TC-登录-002",
                priority="P0",
                title="验证密码错误提示",
                precondition="已注册账号且未被锁定",
                steps=[
                    {"action": "输入错误密码", "expected": "提示「账号或密码错误」"},
                    {"action": "连续错误 5 次", "expected": "账号锁定 30 分钟"},
                ],
            )
        ),
        TestCase.model_validate(
            make_case(case_id="TC-充值-001", module="充值", title="验证微信充值成功")
        ),
    ]
    path = export_xmind(cases, tmp_path / "用例.xmind", root_title="XX活动需求")

    sheets = _load_content(path)
    root = sheets[0]["rootTopic"]
    assert root["title"] == "XX活动需求"

    # 模块分组：登录、充值两个一级子节点
    modules = root["children"]["attached"]
    assert [m["title"] for m in modules] == ["登录", "充值"]
    assert len(modules[0]["children"]["attached"]) == 2

    # 用例节点：优先级在 labels、前置条件在 notes（团队模板约定）
    case2 = modules[0]["children"]["attached"][1]
    assert case2["title"] == "验证密码错误提示"
    assert case2["labels"] == ["P0"]
    assert case2["notes"]["plain"]["content"] == "已注册账号且未被锁定"

    # 步骤 → 预期结果 父子链
    steps = case2["children"]["attached"]
    assert [s["title"] for s in steps] == ["输入错误密码", "连续错误 5 次"]
    assert steps[1]["children"]["attached"][0]["title"] == "账号锁定 30 分钟"


def test_xmind可被zipfile正常读取且无损坏(tmp_path):
    cases = [TestCase.model_validate(make_case())]
    path = export_xmind(cases, tmp_path / "用例.xmind")
    with zipfile.ZipFile(path) as zf:
        assert zf.testzip() is None
        manifest = json.loads(zf.read("manifest.json"))
        assert "content.json" in manifest["file-entries"]
