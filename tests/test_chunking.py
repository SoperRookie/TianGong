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
        seg = user.split("<<<数据:需求原文>>>", 1)[-1]  # 只看需求原文数据块：大纲注入文本里会同时出现两个模块名
        return "功能A" if "功能A" in seg else "功能B"

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


def test_标题行不会被单独切成碎片():
    # 单章节超限按行再切时，标题行会落单；碎片送模型拆不出测试点 → 并入后一片
    text = "# A\n" + "y" * 150 + "\n# B\nshort"
    chunks = split_text(text, 100)
    assert chunks[0].startswith("# A\ny") and all(len(c) >= 5 for c in chunks)
    assert "".join(chunks).count("y") == 150 and any("# B" in c for c in chunks)


# ---------- 整理稿标题大纲 / 按需求分片 / 疑点清单不单独成片 ----------


def _整理稿(title="后台", 区块字数=200):
    big = "- 规则一条，带数值 100 与条件。\n" * (区块字数 // 18 + 1)
    return "\n".join([
        f"# 「{title}」需求整理稿", "", "- 来源：x.zip", "",
        "## 1. 需求说明 〔来源：x.zip › 需求说明〕", "", "### 1.1 版本记录", "- v1", "### 1.2 创建活动", big,
        "## 2. 活动运营后台", "", "### 2.1 活动列表 〔来源：x.zip › 活动运营后台 / 活动列表〕", "",
        "#### 2.1.1 活动列表", "- 列表说明一句", "#### 2.1.2 基本配置", big, "#### 2.1.3 数据配置", big,
        "#### 2.1.4 旧草稿 〔疑似草稿〕", big,
        "### 2.2 派奖查询 〔来源：x.zip › 活动运营后台 / 派奖查询〕", "", "#### 2.2.1 派奖查询", "- 一句",
        "### 2.3 空页面 〔来源：x.zip › 活动运营后台 / 空页面〕", "",
        "## 3. 疑点 / 不一致清单 〔原句已按原文保留在上文，此处只列疑点，供需求澄清与用例设计〕", "", "- 奖池 25% 与 35% 不一致 —— 疑点（活动列表）", "",
        "## 4. 疑似遗留草稿 〔画布上与页面主体不相连、与正式内容重复却有出入的区块，已照抄收录但用例不要照它写，请与产品确认〕", "",
        "- 2.1.4 旧草稿（活动运营后台 / 活动列表-旧草稿）", "", "（已剔除：控件编号）",
    ])


def test_整理稿标题大纲_长区块成模块_短区块并入页面_草稿与疑点清单不进大纲():
    from app.agents.outline import outline_from_headings

    mods = outline_from_headings(_整理稿())
    # 需求说明：短的「版本记录」并入「需求说明」，长的「创建活动」单列；活动列表：长区块单列、「活动列表」兜住短区块
    assert mods == ["需求说明", "需求说明-创建活动", "活动列表", "活动列表-基本配置", "活动列表-数据配置", "派奖查询"]
    # 区块名与页面同名不重复成「派奖查询-派奖查询」；〔疑似草稿〕区块、疑点清单、遗留草稿、空页面都不在
    assert not any("草稿" in m or "疑点" in m or "空页面" in m for m in mods)
    # 不是整理稿格式（没有编号标题）→ 空，交给模型出大纲
    assert outline_from_headings("# 道具系统\n背包规则。\n# 关卡系统\n选关规则。") == []
    assert outline_from_headings(_整理稿(), title="后台")[:2] == ["后台-需求说明", "后台-需求说明-创建活动"]


def test_多需求简报_按需求切段_片不跨需求_疑点清单挂到本需求最后一片():
    from app.agents.outline import APPENDIX_NOTE, outline_from_brief, requirement_chunks, requirement_segments

    a, b = _整理稿("后台", 600), _整理稿("前端", 600)
    brief = f"【需求 1/2：后台】\n【需求整理稿】（AI 摘录）\n{a}\n\n【人工补充说明】\n补充一句\n\n【需求 2/2：前端】\n【需求整理稿】（AI 摘录）\n{b}"
    segs = requirement_segments(brief)
    assert [s.title for s in segs] == ["后台", "前端"] and "补充一句" in segs[0].text and "补充一句" not in segs[1].text
    chunks, titles = requirement_chunks(brief, 1500)
    assert len(chunks) >= 4 and titles[0] == "后台" and titles[-1] == "前端"
    # 每片只含一条需求的内容
    for c, t in zip(chunks, titles):
        other = "前端" if t == "后台" else "后台"
        assert f"「{other}」需求整理稿" not in c
    # 疑点清单 / 遗留草稿不单独成片：带说明挂在本需求最后一片末尾，正文章节仍在前面
    for t in ("后台", "前端"):
        mine = [c for c, tt in zip(chunks, titles) if tt == t]
        assert APPENDIX_NOTE in mine[-1] and "疑点 / 不一致清单" in mine[-1] and "疑似遗留草稿" in mine[-1]
        assert all(APPENDIX_NOTE not in c for c in mine[:-1])
    # 多需求时大纲模块带需求标题前缀，两份整理稿同名章节不混
    mods = outline_from_brief(brief)
    assert "后台-活动列表-基本配置" in mods and "前端-活动列表-基本配置" in mods
    # 整篇不超限：原样一片、无标题（与单需求行为一致）
    assert requirement_chunks(brief, 10**6) == ([brief], [None])
    # 单需求、没有标记：标题 None，疑点清单仍挂到最后一片
    chunks, titles = requirement_chunks(a, 1500)
    assert set(titles) == {None} and APPENDIX_NOTE in chunks[-1]


async def test_整理稿分片拆解_大纲按标题生成不调模型_各片带需求前缀():
    from app.agents.outline import requirement_chunks
    from app.agents.service import run_analysis
    from tests.stubs import StubLLM

    a, b = _整理稿("后台", 600), _整理稿("前端", 600)
    brief = f"【需求 1/2：后台】\n{a}\n\n【需求 2/2：前端】\n{b}"
    n = len(requirement_chunks(brief, 1500)[0])
    reply = json.dumps({"modules": [{"module": "后台-活动列表-基本配置", "points": ["必填"]}], "blind_spots": []}, ensure_ascii=False)
    llm = StubLLM([reply] * n)
    result = await run_analysis(brief, llm, chunk_max_chars=1500)
    assert len(llm.calls) == n   # 没有「长需求模块大纲」那一次调用
    users = [c["messages"][-1]["content"] for c in llm.calls]
    assert all("全文模块大纲" in u and "前端-活动列表-基本配置" in u for u in users)
    assert any("本片属于需求「后台」" in u and "「后台 › 页面或章节 › 需求点」" in u for u in users)
    assert any("本片属于需求「前端」" in u for u in users)
    assert result.test_points[0]["module"] == "后台-活动列表-基本配置"
