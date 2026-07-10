"""大文档分片（F-2-6）：切分策略与并行分段生成合并。"""

import json

from app.agents import run_generation
from app.llm.client import ChatResult
from app.llm.schemas import UsageInfo
from app.parsers.chunking import split_text

# ---------- 纯切分策略 ----------


def test_短文本不分片():
    assert split_text("短需求", 100) == ["短需求"]


def test_按章节边界切分():
    text = "# 章节一\n" + "内容一\n" * 30 + "# 章节二\n" + "内容二\n" * 30
    chunks = split_text(text, 150)
    assert len(chunks) >= 2
    # 章节标题各自完整地落在某个分片开头，不被拦腰截断
    assert any(c.startswith("# 章节一") for c in chunks)
    assert any(c.startswith("# 章节二") for c in chunks)
    # 内容无丢失
    assert sum(c.count("内容一") for c in chunks) == 30
    assert sum(c.count("内容二") for c in chunks) == 30


def test_单章节超限按段落再切():
    text = "# 大章节\n" + "\n".join(f"第{i}段" for i in range(100))
    chunks = split_text(text, 80)
    assert len(chunks) > 1
    assert all(len(c) <= 80 for c in chunks)


# ---------- 并行分段生成与合并 ----------


class RoutingStubLLM:
    """按 Agent 角色与需求内容路由返回，支持并行分片下的确定性响应。"""

    def __init__(self, fail_on: str | None = None, duplicate_title: bool = False):
        self.fail_on = fail_on  # 生成阶段遇到该标记的分片抛错（模拟部分失败）
        self.duplicate_title = duplicate_title

    def _marker(self, user: str) -> str:
        return "功能A" if "功能A" in user else "功能B"

    async def chat(self, messages, model=None, **kw):
        system, user = messages[0]["content"], messages[1]["content"]
        marker = self._marker(user)
        if "测试分析师" in system:
            content = json.dumps(
                {"modules": [{"module": marker, "points": ["正常流"]}], "blind_spots": []},
                ensure_ascii=False,
            )
        elif "测试专家" in system:
            if self.fail_on and self.fail_on in user:
                raise RuntimeError(f"模拟生成失败: {marker}")
            title = "验证通用场景" if self.duplicate_title else f"验证{marker}场景"
            module = "公共模块" if self.duplicate_title else marker
            case = {
                "case_id": f"TC-{module}-001",
                "module": module,
                "title": title,
                "priority": "P1",
                "precondition": "",
                "steps": [{"action": "执行操作", "expected": "返回预期结果"}],
                "remark": "",
            }
            content = json.dumps({"cases": [case]}, ensure_ascii=False)
        else:  # 评审
            content = json.dumps({"passed": True, "issues": [], "missing": []}, ensure_ascii=False)
        return ChatResult(content=content, model_name="stub", provider="stub", usage=UsageInfo(), elapsed_ms=1)


_LONG_REQ = "# 功能A\n" + "A模块规则描述\n" * 20 + "\n# 功能B\n" + "B模块规则描述\n" * 20


async def test_超长需求分片并行生成并合并():
    result = await run_generation(_LONG_REQ, llm=RoutingStubLLM(), chunk_max_chars=200)

    assert result.chunks >= 2
    assert result.passed is True
    modules = {c.module for c in result.cases}
    assert modules == {"功能A", "功能B"}
    # 链路留痕带分片标记
    assert all("chunk" in t for t in result.trace)
    # 测试点按模块合并
    assert {tp["module"] for tp in result.test_points} == {"功能A", "功能B"}


async def test_分片结果去重与重编号():
    # 两个分片生成相同（模块+标题）的用例 → 合并后仅保留一条，编号从 001 连续
    result = await run_generation(_LONG_REQ, llm=RoutingStubLLM(duplicate_title=True), chunk_max_chars=200)

    assert len(result.cases) == 1
    assert result.cases[0].case_id == "TC-公共模块-001"


async def test_分片部分失败不阻塞整体():
    result = await run_generation(_LONG_REQ, llm=RoutingStubLLM(fail_on="功能B"), chunk_max_chars=200)

    assert result.passed is False
    assert any("分片处理失败" in u["problem"] for u in result.unresolved)
    # 成功分片的用例正常产出
    assert {c.module for c in result.cases} == {"功能A"}
