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

    assert analysis.test_points == [{"module": "登录", "points": ["正常登录", "密码错误"]}]
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
