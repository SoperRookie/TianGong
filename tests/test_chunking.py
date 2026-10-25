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
    assert mods == ["需求说明", "需求说明-创建活动", "活动运营后台-活动列表", "活动运营后台-活动列表-基本配置",
                    "活动运营后台-活动列表-数据配置", "活动运营后台-派奖查询"]
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
    assert "后台-活动运营后台-活动列表-基本配置" in mods and "前端-活动运营后台-活动列表-基本配置" in mods
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
    reply = json.dumps({"modules": [{"module": "后台-活动运营后台-活动列表-基本配置", "points": ["必填"]}], "blind_spots": []}, ensure_ascii=False)
    llm = StubLLM([reply] * n)
    result = await run_analysis(brief, llm, chunk_max_chars=1500)
    assert len(llm.calls) == n   # 没有「长需求模块大纲」那一次调用
    users = [c["messages"][-1]["content"] for c in llm.calls]
    assert all("全文模块大纲" in u and "前端-活动运营后台-活动列表-基本配置" in u for u in users)
    assert any("本片属于需求「后台」" in u and "「后台 › 页面或章节 › 需求点」" in u for u in users)
    assert any("本片属于需求「前端」" in u for u in users)
    assert result.test_points[0]["module"] == "后台-活动运营后台-活动列表-基本配置"


def test_文件夹名进模块名_页面名已带文件夹名的不重复():
    from app.agents.outline import outline_from_headings

    big = "- 规则一条，带数值 100 与条件。\n" * 12
    text = "\n".join([
        "# 「前端」需求整理稿", "", "## 3. PC", "", "### 3.1 PC-活动入口说明 〔来源：x.zip › PC / PC-活动入口说明〕", "",
        "#### 3.1.1 入口说明", big, "#### 3.1.2 报名规则说明", big,
        "## 4. 移动", "", "### 4.1 移动-活动入口说明 〔来源：x.zip › 移动 / 移动-活动入口说明〕", "", "#### 4.1.1 入口说明", big,
    ])
    mods = outline_from_headings(text)
    assert mods == ["PC-活动入口说明-入口说明", "PC-活动入口说明-报名规则说明", "移动-活动入口说明-入口说明"]


def test_三片以上小尾巴并入前片_第一片不被覆盖_内容不丢():
    """回归：曾写成 parts[-2] = ... parts.pop()，右边先 pop 使 parts[-2] 变成第一片，第一片被第二片覆盖——
    线上「后台」整理稿的需求说明 / 活动运营后台 / 3.1–3.4 整段没拆、3.4.5 之后拆了两遍。"""
    from app.agents.outline import requirement_chunks

    sec = lambda n, k: f"## {n}. 章节{n}\n" + f"章节{n}的规则第 X 条。\n".replace("X", str(k)) * 1  # noqa: E731
    body = "\n".join(f"## {n}. 章节{n}\n" + "".join(f"- 章节{n}规则{i}\n" for i in range(40)) for n in range(1, 6))
    body += "\n## 6. 小尾巴\n- 尾巴一句"
    brief = f"【需求 1/1：后台】\n{body}"
    chunks, titles = requirement_chunks(brief, 1200)
    assert len(chunks) >= 2 and chunks[0].startswith("【需求 1/1：后台】")
    joined = "\n".join(chunks)
    for n in range(1, 6):
        assert joined.count(f"## {n}. 章节{n}") == 1, n   # 每章恰好出现一次：不丢、不重
    assert joined.count("## 6. 小尾巴") == 1 and "小尾巴" in chunks[-1]


def test_确认结论与建议级假设按模块回到对应片_定不到的放第一片_多需求各自分():
    """线上「连赢10/23」拆解：78 条确认结论（1.3 万字）整块落在只含增量奖池流水的最后一片，那片的模型按
    「只拆本片内容」全跳过，ref 引用确认结论的测试点 0 条。"""
    from app.agents.outline import QA_CHUNK_NOTE, requirement_chunks, split_qa_blocks

    a = _整理稿("后台", 600)
    qa = ("【待确认事项的确认结论（以此为准）】\n"
          "- 问（活动运营后台-活动列表-基本配置）：奖池比例固定吗？\n  确认：固定为 75%／25%\n"
          "- 问（需求说明-创建活动）：活动名称上限？\n  确认：30 个字符\n"
          "- 问：整体是否支持多语言？\n  确认：只有中文\n"
          "- 问（活动列表-数据配置）：模型给的模块不带文件夹名也要对上\n  确认：对得上\n"
          "- 问（活动运营后台-活动列表-基本配置-奖池）：比章节更细的模块归上一级\n  确认：归基本配置\n")
    assume = "【未确认的建议级事项（按下列假设处理，并在用例 remark 中注明假设）】\n- 问（需求说明-创建活动）：时区？\n  假设：UTC+8\n"
    brief = f"【需求整理稿】（AI 摘录）\n{a}\n\n{qa}\n{assume}\n【AI 需求分析要点（已经人工过目）】\n业务规则：一条"
    rest, blocks = split_qa_blocks(brief)
    assert [len(b.items) for b in blocks] == [5, 1] and blocks[0].items[0][0] == "活动运营后台-活动列表-基本配置"
    assert blocks[0].items[2][0] == "" and "确认：固定为 75%／25%" in blocks[0].items[0][1]
    assert "- 问" not in rest and "【AI 需求分析要点" in rest and "## 1. 需求说明" in rest   # 两块摘掉，其余原样

    chunks, _ = requirement_chunks(brief, 1500)
    assert len(chunks) >= 3
    by = lambda s: next(i for i, c in enumerate(chunks) if s in c)  # noqa: E731
    assert by("固定为 75%／25%") == by("#### 2.1.2 基本配置") != by("### 1.2 创建活动")
    assert by("30 个字符") == by("### 1.2 创建活动") == by("假设：UTC+8")
    assert by("只有中文") == 0                                        # 没标模块的整体性问题放第一片
    assert by("对得上") == by("#### 2.1.3 数据配置")                   # 「活动列表-数据配置」对上「活动运营后台-活动列表-数据配置」
    assert by("归基本配置") == by("#### 2.1.2 基本配置")               # 更细的模块名归上一级章节
    # 每片带着块头与说明；条目不在别的片重复出现
    for c in chunks:
        if "确认：" in c:
            assert "【待确认事项的确认结论（以此为准）】" + QA_CHUNK_NOTE in c
    assert sum(c.count("固定为 75%／25%") for c in chunks) == 1
    assert "\n".join(chunks).count("- 问") == 6
    # 分析要点仍在最后一片末尾（没有分到各片）
    assert "【AI 需求分析要点" in chunks[-1]
    # 多需求合并：各自的确认结论只在本需求的片里
    b = _整理稿("前端", 600)
    qb = "【待确认事项的确认结论（以此为准）】\n- 问（活动运营后台-活动列表-基本配置）：前端的比例？\n  确认：前端 50%\n"
    brief2 = f"【需求 1/2：后台】\n{a}\n\n{qa}\n【需求 2/2：前端】\n{b}\n\n{qb}"
    chunks2, titles2 = requirement_chunks(brief2, 1500)
    assert all(t == "后台" for c, t in zip(chunks2, titles2) if "75%／25%" in c)
    assert all(t == "前端" for c, t in zip(chunks2, titles2) if "前端 50%" in c)
    # 整篇不超限：原样一片
    assert requirement_chunks(brief, 10**6) == ([brief], [None])


def test_人工收录小节_去样例与控件文案_规则并入所在页面_空节删掉():
    """线上整理稿 10 个「页面-人工收录」小节模型一条测试点都没拆，里面夹着撤销按钮展示条件、导出文件名这类真规则。"""
    from app.agents.outline import MANUAL_NOTE, clean_manual_sections, outline_from_headings

    text = "\n".join([
        "## 6. 商户管理后台", "", "### 6.7 报名记录", "", "#### 6.7.1 字段说明", "- 会员账号：按权限打码", "",
        "#### 6.7.2 人工收录",
        "| 1 | 2026-08-04 | 测试总账号 | zw9z3vvv1 | 20260227113553 | 2026连赢活动A | 现金报名 | 否 | 已报名 | 68.00 LGC |",
        "|---|---|---|---|---|---|---|---|---|---|",
        "- 活动名称：2026连赢活动 榜单日期：2026-02-09 会员账号：zw9z3vvv1 排名:1 连赢次数：15 积分：133",
        "- 请输入", "- 下一步", "- 搜索　重置", "- 请选择参与终端", "---", "**按从上到下、从左到右的顺序转录：**",
        "- i.只有在榜单日期为未结算时，且报名状态为“已报名、报名中、待生效”时展示“撤销”按钮，其他情况不展示",
        "| 积分变更 | 字符串 | 系统 | 展示本次积分的变动，增加展示+X（X为具体积分），无变动展示- |",
        "- 下拉选项：全部 / 真实 / 机器人",
        "| 运营管理-连赢活动记录 | 会员账号 XXXX | 撤销报名 | 活动ID：XXXX 榜单日期：XXXX 撤销原因：XXXX |",
        "", "### 6.8 月榜排名", "", "#### 6.8.1 功能说明", "- 默认当月", "#### 6.8.2 人工收录",
        "| 2 | 2026-08 | 测试总账号 | zw9z3vvv2 | 20260227113553 | 2026连赢活动A | 17 | 319 | 18,888.00 LGC | 待发 |",
        "- 请输入活动备注（最多支持", "- 20个文字）", "",
        "## 7. 疑点 / 不一致清单", "- 一条疑点",
    ])
    out = clean_manual_sections(text)
    assert "人工收录" not in out and out.count(MANUAL_NOTE) == 1          # 6.7.2 留规则换成说明行；6.8.2 清空整节删掉
    assert "展示“撤销”按钮" in out and "积分变更 | 字符串" in out and "下拉选项：全部 / 真实 / 机器人" in out
    assert "撤销原因：XXXX" in out                                        # XXXX 是占位不是样例数据
    for gone in ("zw9z3vvv1", "18,888.00", "排名:1 连赢次数：15", "请输入", "下一步", "搜索　重置", "请选择参与终端", "转录", "20个文字"):
        assert gone not in out, gone
    assert out.index("- 会员账号：按权限打码") < out.index(MANUAL_NOTE) < out.index("### 6.8 月榜排名")   # 留在本页面末尾
    assert "## 7. 疑点 / 不一致清单\n- 一条疑点" in out and "#### 6.8.1 功能说明\n- 默认当月" in out
    assert not any("人工收录" in m for m in outline_from_headings(out))
    plain = "## 1. 登录\n- 规则\n### 1.1 密码\n- 6 位"
    assert clean_manual_sections(plain) == plain                          # 没有这种小节：原样


async def test_拆解入口先清人工收录_模型看到的是并入页面的规则():
    from app.agents.outline import MANUAL_NOTE
    from app.agents.service import run_analysis
    from tests.stubs import StubLLM

    text = "## 1. 报名记录\n- 会员账号按权限打码\n### 1.1 人工收录\n- 请输入\n- 只有未结算时展示撤销按钮\n"
    llm = StubLLM([json.dumps({"modules": [{"module": "报名记录", "points": ["打码"]}], "blind_spots": []}, ensure_ascii=False)])
    await run_analysis(text, llm, chunk_max_chars=10**6)
    user = llm.calls[0]["messages"][-1]["content"]
    assert "人工收录" not in user and "- 请输入" not in user
    assert MANUAL_NOTE in user and "只有未结算时展示撤销按钮" in user
