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
