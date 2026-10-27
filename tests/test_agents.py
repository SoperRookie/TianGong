"""三角色编排回环行为测试（桩 LLM，不消耗 token）。"""

from app.agents import run_generation
from app.agents.graph import rule_check
from tests.stubs import ANALYST_REPLY, StubLLM, generator_reply, make_case, review_reply

_case = make_case


async def test_一次评审通过():
    llm = StubLLM([ANALYST_REPLY, generator_reply(_case()), review_reply(True)])
    result = await run_generation("登录需求", llm=llm)

    assert result.passed is True
    assert result.review_rounds == 1
    assert result.cases[0].case_id == "TC-登录-001"
    assert result.blind_spots == ["未说明锁定策略"]
    agents = [t["agent"] for t in result.trace]
    assert agents == ["需求分析", "用例生成", "评审"]


async def test_评审打回定点修正后通过():
    llm = StubLLM(
        [
            ANALYST_REPLY,
            generator_reply(_case(title="验证登录")),
            review_reply(False, [{"case_id": "TC-登录-001", "problem": "预期结果不具体"}]),
            generator_reply(_case()),
            review_reply(True),
        ]
    )
    result = await run_generation("登录需求", llm=llm)

    assert result.passed is True
    assert result.review_rounds == 2
    # 修正轮的用户消息应携带评审问题（定点打回）
    fix_call = llm.calls[3]
    assert "预期结果不具体" in fix_call["messages"][1]["content"]


async def test_三轮上限强制出稿并标注未解决项():
    issue = [{"case_id": "TC-登录-001", "problem": "覆盖不足"}]
    llm = StubLLM(
        [
            ANALYST_REPLY,
            generator_reply(_case()),
            review_reply(False, issue),
            generator_reply(_case()),
            review_reply(False, issue),
            generator_reply(_case()),
            review_reply(False, issue),
        ]
    )
    result = await run_generation("登录需求", llm=llm)

    assert result.passed is False
    assert result.review_rounds == 3
    assert result.unresolved and result.unresolved[0]["problem"] == "覆盖不足"
    assert len(result.cases) == 1  # 强制出稿仍保留可用用例


async def test_评审模型可与生成模型不同():
    llm = StubLLM([ANALYST_REPLY, generator_reply(_case()), review_reply(True)])
    await run_generation("登录需求", llm=llm, model="deepseek-chat", reviewer_model="deepseek-reasoner")

    assert llm.calls[0]["model"] == "deepseek-chat"  # 拆解
    assert llm.calls[1]["model"] == "deepseek-chat"  # 生成
    assert llm.calls[2]["model"] == "deepseek-reasoner"  # 评审独立模型


def test_规则校验_优先级非法与编号重复():
    bad = [_case(priority="P5"), _case(), _case()]  # P5 非法 + 后两条编号重复
    issues = rule_check(bad)
    problems = " ".join(i["problem"] for i in issues)
    assert "模板校验不通过" in problems
    assert "编号重复" in problems


def test_规则校验_通过():
    assert rule_check([_case(), _case(case_id="TC-登录-002")]) == []


def test_规则校验_编号跳号():
    # 001 之后直接 003，缺 002
    issues = rule_check([_case(), _case(case_id="TC-登录-003")])
    assert any("不连续" in i["problem"] for i in issues)


async def test_已确认测试点跳过拆解直接生成():
    # 只准备生成+评审两个响应：若图仍从拆解开始会因响应不足失败
    llm = StubLLM([generator_reply(_case()), review_reply(True)])
    points = [{"module": "登录", "points": ["用户修改后的测试点"]}]
    result = await run_generation("登录需求", llm=llm, test_points=points)

    assert result.passed is True
    agents = [t["agent"] for t in result.trace]
    assert "需求分析" not in agents  # 拆解已被跳过
    # 生成 Prompt 中携带的是用户确认后的测试点
    assert "用户修改后的测试点" in llm.calls[0]["messages"][1]["content"]


async def test_多模块确认后按模块并行生成():
    from tests.test_chunking import RoutingStubLLM

    points = [
        {"module": "功能A", "points": ["A点"]},
        {"module": "功能B", "points": ["B点"]},
    ]
    result = await run_generation("需求全文", llm=RoutingStubLLM(), test_points=points)

    assert result.passed is True
    assert {c.module for c in result.cases} == {"功能A", "功能B"}
    assert result.chunks == 1  # 并行维度是模块，不是文档分片


async def test_run_analysis_仅拆解():
    from app.agents import run_analysis

    llm = StubLLM([ANALYST_REPLY])
    analysis = await run_analysis("登录需求", llm=llm)

    # 测试点归一化为对象形态（生成质量核心需求：测试点带维度标注）
    assert analysis.test_points == [
        {"module": "登录", "points": [
            {"point": "正常登录", "dimension": ""}, {"point": "密码错误", "dimension": ""},
        ]}
    ]
    assert analysis.blind_spots == ["未说明锁定策略"]
    assert len(llm.calls) == 1  # 只有拆解调用，无生成/评审


# ---- 定点修正增量协议（只回传改动，服务端合并）----


def test_merge_fix_incremental_replace_add_delete():
    from app.agents.graph import merge_fix

    current = [
        make_case(),
        make_case(case_id="TC-登录-002", title="验证密码错误提示"),
        make_case(case_id="TC-登录-003", title="验证锁定策略"),
    ]
    data = {
        "cases": [
            make_case(case_id="TC-登录-002", title="验证密码错误提示", priority="P0"),  # 修改
            make_case(case_id="TC-登录-004", title="验证验证码过期"),  # 新增
        ],
        "deleted": ["TC-登录-003"],
    }
    merged = merge_fix(current, data)
    assert [c["title"] for c in merged] == [
        "验证正确账号密码登录成功", "验证密码错误提示", "验证验证码过期",
    ]
    assert merged[1]["priority"] == "P0"  # 改动覆盖
    assert merged[0]["priority"] == "P1"  # 未改动原样保留
    assert [c["case_id"] for c in merged] == ["TC-登录-001", "TC-登录-002", "TC-登录-003"]  # 重排连续


def test_merge_fix_full_echo_still_works():
    """模型不守增量协议、仍回传全集时，合并结果等价于全量覆盖（向后兼容）。"""
    from app.agents.graph import merge_fix

    current = [make_case(), make_case(case_id="TC-登录-002", title="验证密码错误提示")]
    echo = {"cases": [dict(c, priority="P2") for c in current]}
    merged = merge_fix(current, echo)
    assert len(merged) == 2 and all(c["priority"] == "P2" for c in merged)


# ---- 畸形 JSON 抢救（模型长输出偶发 token 跳漏）----


def test_extract_json_salvages_partially_corrupt_cases():
    from app.agents.json_utils import extract_json

    # 复刻线上故障：第 1 条用例中段丢字（标题直接跳到 precondition 中间），后续用例完好
    corrupt = (
        '{"cases": [{"case_id": "TC-游戏规则-001", "module": "游戏规则", '
        '"title": "验证游戏ondition": "进入房间", "steps": [{"action": "开局", "expected": "正常"}]}, '
        '{"case_id": "TC-游戏规则-002", "module": "游戏规则", "title": "验证发牌", "priority": "P0", '
        '"precondition": "已开局", "steps": [{"action": "等待发牌", "expected": "6门手牌各2张"}], '
        '"remark": "", "extras": {}}]}'
    )
    data = extract_json(corrupt)
    assert [c["case_id"] for c in data["cases"]] == ["TC-游戏规则-002"]
    # 内嵌 steps 对象不会被误认为用例
    assert all("action" not in c for c in data["cases"])


def test_extract_json_salvages_corrupt_analysis():
    from app.agents.json_utils import extract_json

    corrupt = (
        '{"modules": [{"module": "投注", "points": ["正常投注", "超限投注"]}, '
        '{"module": "结算", "poi<糟糕的输出>], "blind_spots": ["未说明超时"]}'
    )
    data = extract_json(corrupt)
    assert data["modules"] == [{"module": "投注", "points": ["正常投注", "超限投注"]}]


def test_extract_json_still_raises_on_hopeless_output():
    import pytest as _pytest
    from app.agents.json_utils import LLMOutputError, extract_json

    with _pytest.raises(LLMOutputError):
        extract_json("完全不是 JSON 的输出")


async def test_长需求分片拆解_先出全文大纲再注入各片():
    import json

    from app.agents.service import run_analysis
    from tests.stubs import StubLLM

    outline = json.dumps({"modules": ["道具系统-背包", "关卡系统-选关"]}, ensure_ascii=False)
    a1 = json.dumps({"modules": [{"module": "道具系统-背包", "points": ["切换分类"]}], "blind_spots": []}, ensure_ascii=False)
    a2 = json.dumps({"modules": [{"module": "关卡系统-选关", "points": ["选关进入"]}], "blind_spots": []}, ensure_ascii=False)
    llm = StubLLM([outline, a1, a2])
    text = "# 道具系统\n" + "背包规则。\n" * 300 + "# 关卡系统\n" + "选关规则。\n" * 300
    result = await run_analysis(text, llm, chunk_max_chars=2000)
    assert [m["module"] for m in result.test_points] == ["道具系统-背包", "关卡系统-选关"]
    assert len(llm.calls) == 3
    first_user = llm.calls[1]["messages"][-1]["content"]
    assert "全文模块大纲" in first_user and "第 1/2 片" in first_user and "道具系统-背包" in first_user
    # 大纲用 low、拆解用 high
    assert llm.calls[0].get("reasoning_effort", None) in (None, "low")


def test_normalize_points_兼容键名别名与顶层数组():
    from app.agents.json_utils import extract_json
    from app.tasks.points import extract_modules, normalize_points

    # 模块名 / 测试点列表 / 测试点文本 / 维度 使用别名
    mods = normalize_points([
        {"name": "登录", "test_points": [{"description": "正常登录", "type": "正常流程"}, {"test_point": "密码错误"}, "字符串点"]},
        {"模块": "注册", "测试点": [{"title": "验证码过期", "维度": "异常流程"}]},
        {"module": "空模块", "points": [{"foo": "没有文本键"}]},
    ])
    assert mods[0]["module"] == "登录" and [p["point"] for p in mods[0]["points"]] == ["正常登录", "密码错误", "字符串点"]
    assert mods[0]["points"][0]["dimension"] == "正常流程"
    assert mods[1] == {"module": "注册", "points": [{"point": "验证码过期", "dimension": "异常流程"}]}
    assert mods[2]["points"] == []
    # 顶层数组 / 别名键 / result 包装 / 模块名→列表 的字典形态
    assert extract_modules([{"module": "A", "points": ["x"]}])[0]["module"] == "A"
    assert extract_modules({"test_points": [{"module": "A", "points": ["x"]}]})[0]["module"] == "A"
    assert extract_modules({"result": {"modules": [{"module": "A", "points": ["x"]}]}})[0]["module"] == "A"
    assert normalize_points(extract_modules({"modules": {"登录": ["正常登录"], "注册": ["正常注册"]}}))[1]["points"][0]["point"] == "正常注册"
    assert extract_modules({"foo": "bar"}) == []
    assert extract_json('[{"module": "A", "points": ["x"]}]') == {"modules": [{"module": "A", "points": ["x"]}]}


async def test_拆解结果格式改写仍能解析_完全异常时报错带诊断():
    import json

    import pytest

    from app.agents.graph import analyze_requirement
    from app.agents.json_utils import LLMOutputError

    reply = json.dumps({"result": {"test_points": [{"name": "登录", "items": [{"description": "正常登录", "type": "正常流程"}]}],
                                   "blind_spots": [{"description": "未说明锁定策略"}]}}, ensure_ascii=False)
    llm = StubLLM([reply])
    a = await analyze_requirement(llm, "登录需求", None)
    assert a["test_points"] == [{"module": "登录", "points": [{"point": "正常登录", "dimension": "正常流程"}]}]
    assert a["blind_spots"] == ["未说明锁定策略"]  # 对象形态 + result 包装
    empty = json.dumps({"summary": "无法拆解", "modules": []})
    llm = StubLLM([empty, empty])  # 空结果先重试一次，两次都空才报错
    with pytest.raises(LLMOutputError) as e:
        await analyze_requirement(llm, "登录需求", None)
    assert len(llm.calls) == 2
    assert "顶层键 [summary、modules]" in str(e.value) and "stub" in str(e.value)
    # 第一次空、第二次正常 → 成功
    llm = StubLLM([empty, ANALYST_REPLY])
    a = await analyze_requirement(llm, "登录需求", None)
    assert a["test_points"][0]["module"] == "登录"


async def test_分片拆解一片失败立即取消其余分片_不再继续调用模型():
    import asyncio

    import pytest

    from app.agents import run_analysis
    from app.agents.json_utils import LLMOutputError
    from app.llm.client import ChatResult, UsageInfo

    class SlowLLM:
        """第一片两次（含重试）都返回垃圾 → 失败；其余分片的调用在睡眠中应被取消，不得跑到完成。"""
        started = 0
        finished = 0
        cancelled = 0

        async def chat(self, messages, model=None, **kw):
            if "子系统清单" in messages[0]["content"]:  # 全文大纲调用：即时返回
                return ChatResult(content='{"modules": ["模块0"]}', model_name="stub", provider="stub", usage=UsageInfo(), elapsed_ms=1)
            SlowLLM.started += 1
            user = messages[-1]["content"]
            if "第 1/" in user:
                content = "不是 JSON"
            else:
                try:
                    await asyncio.sleep(1.0)
                except asyncio.CancelledError:
                    SlowLLM.cancelled += 1
                    raise
                SlowLLM.finished += 1
                content = ANALYST_REPLY
            return ChatResult(content=content, model_name="stub", provider="stub", usage=UsageInfo(), elapsed_ms=1)

    text = "\n".join(f"# 模块{i}\n" + "规则。" * 300 for i in range(4))
    with pytest.raises(LLMOutputError):
        await run_analysis(text, SlowLLM(), chunk_max_chars=1200)
    await asyncio.sleep(0.05)
    assert SlowLLM.started >= 3 and SlowLLM.finished == 0 and SlowLLM.cancelled >= 1


class _ChunkLLM:
    """分片拆解路由：按「第 N/M 片」决定返回内容；empty_chunks 中的分片返回空模块。"""

    def __init__(self, empty_chunks=()):
        self.empty = set(empty_chunks)
        self.calls = 0

    async def chat(self, messages, model=None, **kw):
        import json
        import re

        from app.llm.client import ChatResult, UsageInfo

        self.calls += 1
        if "子系统清单" in messages[0]["content"]:
            content = json.dumps({"modules": ["模块0", "模块1", "模块2"]}, ensure_ascii=False)
        else:
            m = re.search(r"第 (\d+)/\d+ 片", messages[-1]["content"])
            no = int(m.group(1)) if m else 0
            content = json.dumps({"modules": [] if no in self.empty else
                                  [{"module": f"模块{no - 1}", "points": [f"第{no}片的测试点"]}], "blind_spots": []},
                                 ensure_ascii=False)
        return ChatResult(content=content, model_name="stub", provider="stub", usage=UsageInfo(), elapsed_ms=1)


async def test_分片拆解_单片为空跳过_全部为空才报错():
    import pytest

    from app.agents import run_analysis
    from app.agents.json_utils import LLMOutputError

    text = "\n".join(f"# 模块{i}\n" + "规则。" * 300 for i in range(3))
    llm = _ChunkLLM(empty_chunks={2})
    result = await run_analysis(text, llm, chunk_max_chars=1200)
    assert {tp["module"] for tp in result.test_points} == {"模块0", "模块2"}
    assert [t.get("empty") for t in result.trace] == [None, True, None]
    assert llm.calls == 4  # 大纲 1 次 + 3 片各 1 次：分片模式下空结果不重试
    with pytest.raises(LLMOutputError, match="均为空"):
        await run_analysis(text, _ChunkLLM(empty_chunks={1, 2, 3}), chunk_max_chars=1200)


class _BlindSpotChunkLLM:
    """分片拆解 + 盲区复核路由桩：第 1 片标出两条盲区，第 3 片原文说明了其中一条。"""

    def __init__(self, verify_fail=False):
        self.verify_fail = verify_fail
        self.verify_calls = 0

    async def chat(self, messages, model=None, **kw):
        import json
        import re

        from app.llm.client import ChatResult, UsageInfo

        system, user = messages[0]["content"], messages[-1]["content"]
        if "子系统清单" in system:
            content = json.dumps({"modules": ["模块0", "模块1", "模块2"]}, ensure_ascii=False)
        elif "盲区清单" in user:
            self.verify_calls += 1
            if self.verify_fail:
                raise RuntimeError("模型不可用")
            m = re.search(r"需求原文（第 (\d+)/\d+ 片）", user)
            explained = [{"index": 1, "evidence": "第 3 片：锁定 30 分钟后自动解锁"}] if m and m.group(1) == "3" else []
            content = json.dumps({"explained": explained}, ensure_ascii=False)
        else:
            m = re.search(r"第 (\d+)/\d+ 片", user)
            no = int(m.group(1)) if m else 0
            spots = ["未说明锁定时长", "未说明找回密码入口"] if no == 1 else []
            content = json.dumps({"modules": [{"module": f"模块{no - 1}", "points": [f"第{no}片的测试点"]}],
                                  "blind_spots": spots}, ensure_ascii=False)
        return ChatResult(content=content, model_name="stub", provider="stub", usage=UsageInfo(), elapsed_ms=1)


async def test_分片盲区复核_后面分片已说明的不算盲区():
    from app.agents import run_analysis

    text = "\n".join(f"# 模块{i}\n" + "规则。" * 300 for i in range(3))
    llm = _BlindSpotChunkLLM()
    result = await run_analysis(text, llm, chunk_max_chars=1200)
    assert llm.verify_calls == 3  # 每片各复核一次
    assert result.blind_spots == ["未说明找回密码入口"]
    assert result.resolved_blind_spots == [{"text": "未说明锁定时长", "evidence": "第 3 片：锁定 30 分钟后自动解锁", "chunk": 3}]
    assert result.trace[-1] == {"agent": "盲区复核", "checked": 2, "removed": 1}
    # 复核失败：盲区原样保留，不阻塞拆解
    result = await run_analysis(text, _BlindSpotChunkLLM(verify_fail=True), chunk_max_chars=1200)
    assert result.blind_spots == ["未说明锁定时长", "未说明找回密码入口"] and result.resolved_blind_spots == []


async def test_单片拆解不做盲区复核():
    from app.agents import run_analysis

    llm = StubLLM([ANALYST_REPLY])
    result = await run_analysis("短需求", llm)
    assert len(llm.calls) == 1 and result.blind_spots == ["未说明锁定策略"]


async def test_UI检查类用例不生成_兜底剔除并重编号():
    from app.agents.graph import is_ui_check_case

    ui = make_case("TC-登录-001", title="登录页按钮颜色与布局符合设计稿")
    biz = make_case("TC-登录-002", title="密码错误 5 次后账号锁定 30 分钟")
    ui2 = make_case("TC-登录-003", title="横竖屏切换后登录表单显示正常")
    llm = StubLLM([ANALYST_REPLY, generator_reply(ui, biz, ui2), review_reply(True)])
    result = await run_generation("登录需求", llm=llm)
    assert [c.title for c in result.cases] == ["密码错误 5 次后账号锁定 30 分钟"]
    assert result.cases[0].case_id == "TC-登录-001"  # 剔除后重编号保持连续
    filt = [t for t in result.trace if t.get("agent") == "UI 检查过滤"]
    assert filt and filt[0]["removed"] == 2 and "登录页按钮颜色与布局符合设计稿" in filt[0]["titles"]
    # 常与业务结果同现的词不误判
    for title in ["余额不足时下注按钮置灰不可点击", "首页展示当前余额 80 元", "提示文案为「账号已锁定」", "无权限用户看不到删除按钮"]:
        assert not is_ui_check_case({"title": title}), title
    for title in ["页面字体字号符合规范", "弹窗居中对齐且圆角 8px", "列表项间距一致", "UI 展示正常"]:
        assert is_ui_check_case({"title": title}), title


async def test_生成结果全部为UI检查类时报错():
    import pytest as _pt
    from app.agents.json_utils import LLMOutputError

    ui = make_case("TC-登录-001", title="登录页整体样式与设计稿一致")
    llm = StubLLM([ANALYST_REPLY, generator_reply(ui), review_reply(True)])
    with _pt.raises(LLMOutputError, match="UI 检查类"):
        await run_generation("登录需求", llm=llm)


async def test_测试点带来源需求点_拆解到确认生成全程保留():
    import json
    from app.tasks.points import add_points, confirmable_points, normalize_points

    reply = json.dumps({"modules": [{"module": "登录页", "points": [
        {"point": "密码错误 5 次锁定 30 分钟", "dimension": "异常流程", "ref": "3.2 登录失败处理"},
        {"point": "正常登录", "dimension": "正常流程", "需求点": "3.1 登录"},
        {"point": "没有来源的点", "dimension": "正常流程"}]}], "blind_spots": []}, ensure_ascii=False)
    llm = StubLLM([reply, generator_reply(_case()), review_reply(True)])
    result = await run_generation("登录需求", llm=llm)
    pts = result.test_points[0]["points"]
    assert pts[0]["ref"] == "3.2 登录失败处理" and pts[1]["ref"] == "3.1 登录" and "ref" not in pts[2]
    # 实体化 / 修改 / 确认生成都保留；过长截断
    mods = normalize_points([{"module": "登录页", "points": []}])
    added = add_points(mods, [{"module": "登录页", "point": "新点", "ref": "x" * 200}, {"module": "登录页", "point": "无来源"}], source="gap")
    assert len(added[0]["ref"]) == 120 and "ref" not in added[1]
    for p in mods[0]["points"]:
        p["status"] = "approved"
    out = confirmable_points(mods)[0]["points"]
    assert out[0]["ref"] == "x" * 120 and "ref" not in out[1]
    # 生成 Agent 收到的测试点带 ref
    assert "3.2 登录失败处理" in llm.calls[1]["messages"][-1]["content"]


async def test_推理耗尽输出预算没有正文时重试一次_有正文被截断不重试():
    import pytest

    from app.agents.graph import _chat_json
    from app.agents.json_utils import LLMOutputError
    from app.llm.client import ChatResult
    from app.llm.schemas import UsageInfo

    class LenStub:
        def __init__(self, replies):
            self.replies, self.calls = list(replies), []

        async def chat(self, messages, model=None, **kw):
            self.calls.append(messages)
            content, finish = self.replies.pop(0)
            return ChatResult(content=content, model_name="stub", provider="stub", usage=UsageInfo(), elapsed_ms=1, finish_reason=finish)

    # 第一次 length 且无正文（推理吃光预算）→ 原样再试一次 → 成功
    llm = LenStub([("", "length"), ('{"modules": []}', "stop")])
    data, _ = await _chat_json(llm, [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}], None)
    assert data == {"modules": []} and len(llm.calls) == 2
    # 有正文但被截断：不重试，直接报错
    llm = LenStub([('{"modules": [{"module": "A", "poin', "length")])
    with pytest.raises(LLMOutputError):
        await _chat_json(llm, [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}], None)
    assert len(llm.calls) == 1


# ---- 经验延伸：拆完需求明文再按资深测试经验补需求没写到的场景 ----


EXTEND_REPLY = __import__("json").dumps({"extensions": [
    {"module": "登录", "point": "登录请求发出后断网再恢复，不重复登录也不卡在加载中", "dimension": "网络",
     "ref": "登录 › 登录按钮", "basis": "弱网下请求重发导致重复登录是常见缺陷"},
    {"module": "登录", "point": "密码错误", "dimension": "异常流程", "ref": "登录 › 密码", "basis": "复述明文"},
    {"module": "登录", "point": "两台设备同时登录同一账号，后登录的把先登录的踢下线", "dimension": "重复操作", "ref": "登录 › 登录", "basis": ""},
    {"module": "支付", "point": "支付中途切后台再回来订单状态正确", "dimension": "中断", "ref": "支付 › 支付", "basis": "需求没有支付模块"},
    {"module": "登录页", "point": "登录中切后台再回到前台，登录结果仍正确展示", "dimension": "中断",
     "ref": "登录 › 登录按钮", "basis": "切后台丢回调是移动端常见缺陷"},
    {"module": "登录", "point": "账号前后带空格登录，按去空格后的账号处理", "dimension": "输入校验",
     "ref": "登录 › 账号输入框", "basis": "复制粘贴带空格是高频投诉"},
    {"module": "登录", "point": "连续快速点击登录按钮 5 次只发出一次登录请求", "dimension": "重复操作",
     "ref": "登录 › 登录按钮", "basis": "重复提交"},
]}, ensure_ascii=False)


async def test_经验延伸_拆完明文再补需求之外的场景_标来源与依据_复述没依据模块不对超上限的跳过_关掉不调用():
    from app.agents import run_analysis

    llm = StubLLM([ANALYST_REPLY, EXTEND_REPLY])
    analysis = await run_analysis("登录需求", llm=llm, extend=True)
    assert len(llm.calls) == 2
    ext_user = llm.calls[1]["messages"][-1]["content"]
    assert "需求原文" in ext_user and "正常登录" in ext_user and "密码错误" in ext_user   # 明文点一并给到，延伸不许复述
    assert "需求没写、但有经验的测试人员一定会去测" in llm.calls[1]["messages"][0]["content"]
    points = analysis.test_points[0]["points"]
    spec = [p for p in points if p.get("source") != "derived"]
    derived = [p for p in points if p.get("source") == "derived"]
    assert [p["point"] for p in spec] == ["正常登录", "密码错误"]            # 明文点原样在前
    # 7 条里：复述「密码错误」跳过、没依据跳过、模块「支付」对不上跳过、「登录页」挂到「登录」、上限 max(3, 2/3)=3 → 第 4 条合格的跳过
    assert [p["point"][:6] for p in derived] == ["登录请求发出", "登录中切后台", "账号前后带空"]
    assert all(p["basis"] and p["ref"] for p in derived)
    assert derived[0]["dimension"] == "网络" and derived[0]["basis"].startswith("弱网")
    ext_trace = [t for t in analysis.trace if t["agent"] == "经验延伸"]
    assert ext_trace == [{"agent": "经验延伸", "model": "stub", "added": 3, "skipped": 4}]

    llm = StubLLM([ANALYST_REPLY])
    analysis = await run_analysis("登录需求", llm=llm, extend=False)
    assert len(llm.calls) == 1 and all("basis" not in p for p in analysis.test_points[0]["points"])
    analysis = await run_analysis("登录需求", llm=StubLLM([ANALYST_REPLY]))   # 测试环境配置关着：默认不延伸
    assert all("basis" not in p for p in analysis.test_points[0]["points"])


async def test_经验延伸故障只丢延伸_明文测试点照常返回():
    from app.agents import run_analysis

    llm = StubLLM([ANALYST_REPLY, "不是 JSON 的输出"])   # 解析失败重试一次时桩已空 → 延伸异常
    analysis = await run_analysis("登录需求", llm=llm, extend=True)
    assert [p["point"] for p in analysis.test_points[0]["points"]] == ["正常登录", "密码错误"]
    assert any(t["agent"] == "经验延伸" and t.get("error") for t in analysis.trace)


async def test_经验延伸点进生成_带依据让生成按依据写用例():
    llm = StubLLM([generator_reply(_case()), review_reply(True)])
    points = [{"module": "登录", "points": [
        {"point": "正常登录", "dimension": "正常流程", "tp_id": "TP001"},
        {"point": "登录请求发出后断网再恢复，不重复登录", "dimension": "网络", "tp_id": "TP002",
         "basis": "弱网下请求重发导致重复登录是常见缺陷", "source": "derived"},
    ]}]
    await run_generation("登录需求", llm=llm, test_points=points)
    gen_user = llm.calls[0]["messages"][1]["content"]
    assert "弱网下请求重发导致重复登录是常见缺陷" in gen_user
    assert "经验延伸测试点" in llm.calls[0]["messages"][0]["content"]


async def test_直接生成模式也走经验延伸_延伸点进同一模块一起生成():
    llm = StubLLM([ANALYST_REPLY, EXTEND_REPLY, generator_reply(_case()), review_reply(True)])
    result = await run_generation("登录需求", llm=llm, extend=True)
    assert result.passed is True
    assert [t["agent"] for t in result.trace][:2] == ["需求分析", "经验延伸"]
    gen_user = llm.calls[2]["messages"][1]["content"]
    assert "登录请求发出后断网再恢复" in gen_user and "弱网下请求重发" in gen_user


async def test_多模块生成只带本模块所在章节_评审需求原文在前可缓存_关掉恢复整篇(monkeypatch):
    """线上 7.6 万字需求 × 111 模块：每个模块的生成与每轮评审都带整份需求，同一份需求被送几百遍。"""
    import app.agents.quality as Q
    from app.config import get_settings
    from app.llm.calllog import record_call
    from app.llm.client import ChatResult
    from app.llm.schemas import UsageInfo
    from app.prompts import prompt_text
    from tests.test_quality_loop import _整理稿_for_gap

    monkeypatch.setattr(Q, "GAP_BATCH_CHARS", 1500)
    req = _整理稿_for_gap()
    points = [
        {"module": "需求说明-创建活动", "points": [{"tp_id": "TP001", "point": "活动名称必填", "ref": "1.1 创建活动 › 名称"}]},
        {"module": "活动运营后台-派奖查询", "points": [{"tp_id": "TP003", "point": "待派可查", "ref": "2.2.1 派奖查询 › 状态"}]},
    ]

    class Routing:
        def __init__(self):
            self.calls: list[list[dict]] = []

        async def chat(self, messages, model=None, **kw):
            self.calls.append(messages)
            system, user = messages[0]["content"], messages[-1]["content"]
            if system.startswith(prompt_text("reviewer")[:60]):
                content = review_reply(True)
            else:
                module = "需求说明-创建活动" if "TP001" in user else "活动运营后台-派奖查询"
                content = generator_reply(make_case(case_id=f"TC-{module}-001", module=module, title=module))
            result = ChatResult(content=content, model_name="stub", provider="stub", usage=UsageInfo(), elapsed_ms=1)
            record_call(messages, result)
            return result

    llm = Routing()
    result = await run_generation(req, llm=llm, test_points=points)
    assert result.passed and {c.module for c in result.cases} == {"需求说明-创建活动", "活动运营后台-派奖查询"}
    gens = [m[-1]["content"] for m in llm.calls if not m[0]["content"].startswith(prompt_text("reviewer")[:60])]
    revs = [m[-1]["content"] for m in llm.calls if m[0]["content"].startswith(prompt_text("reviewer")[:60])]
    assert len(gens) == 2 and len(revs) == 2
    a = next(u for u in gens if "TP001" in u)
    b = next(u for u in gens if "TP003" in u)
    # 各自只带所在章节：创建活动那片没有派奖查询章节，反之亦然；标签注明只含本模块章节
    assert "### 1.1 创建活动" in a and "#### 2.2.1 派奖查询" not in a and "只含本模块所在章节" in a
    assert "#### 2.2.1 派奖查询" in b and "### 1.1 创建活动" not in b
    assert len(a) < len(req) and len(b) < len(req)
    # 评审：需求原文在 user 消息最前，评审范围与轮次在后（各轮共享前缀命中缓存）
    r = revs[0]
    assert r.startswith("需求内容（只含本模块所在章节的原文") and r.index("本次评审范围仅限模块") > r.index("需求原文")
    # 关掉配置：两个模块都带整篇，不加注明
    monkeypatch.setattr(get_settings(), "generation_module_scope", False)
    llm = Routing()
    await run_generation(req, llm=llm, test_points=points)
    gens = [m[-1]["content"] for m in llm.calls if not m[0]["content"].startswith(prompt_text("reviewer")[:60])]
    assert all("### 1.1 创建活动" in u and "#### 2.2.1 派奖查询" in u and "只含本模块所在章节" not in u for u in gens)
