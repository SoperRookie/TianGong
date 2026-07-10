"""三角色编排回环行为测试（桩 LLM，不消耗 token）。"""

import json

from app.agents import run_generation
from app.agents.graph import rule_check
from app.llm.schemas import UsageInfo


def _case(case_id="TC-登录-001", priority="P1", **kw):
    base = {
        "case_id": case_id,
        "module": "登录",
        "title": "验证正确账号密码登录成功",
        "priority": priority,
        "precondition": "已注册账号",
        "steps": [{"action": "输入正确账号密码并提交", "expected": "跳转首页并展示用户昵称"}],
        "remark": "",
    }
    base.update(kw)
    return base


ANALYST_REPLY = json.dumps(
    {"modules": [{"module": "登录", "points": ["正常登录", "密码错误"]}], "blind_spots": ["未说明锁定策略"]},
    ensure_ascii=False,
)


def _generator_reply(*cases):
    return json.dumps({"cases": list(cases)}, ensure_ascii=False)


def _review_reply(passed, issues=()):
    return json.dumps({"passed": passed, "issues": list(issues), "missing": []}, ensure_ascii=False)


class StubLLM:
    """按调用顺序返回预置内容，兼容 LLMClient.chat 接口。"""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def chat(self, messages, model=None, require_vision=False, **overrides):
        self.calls.append({"messages": messages, "model": model})
        from app.llm.client import ChatResult

        return ChatResult(
            content=self.replies.pop(0),
            model_name=model or "stub",
            provider="stub",
            usage=UsageInfo(),
            elapsed_ms=1,
        )


async def test_一次评审通过():
    llm = StubLLM([ANALYST_REPLY, _generator_reply(_case()), _review_reply(True)])
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
            _generator_reply(_case(title="验证登录")),
            _review_reply(False, [{"case_id": "TC-登录-001", "problem": "预期结果不具体"}]),
            _generator_reply(_case()),
            _review_reply(True),
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
            _generator_reply(_case()),
            _review_reply(False, issue),
            _generator_reply(_case()),
            _review_reply(False, issue),
            _generator_reply(_case()),
            _review_reply(False, issue),
        ]
    )
    result = await run_generation("登录需求", llm=llm)

    assert result.passed is False
    assert result.review_rounds == 3
    assert result.unresolved and result.unresolved[0]["problem"] == "覆盖不足"
    assert len(result.cases) == 1  # 强制出稿仍保留可用用例


async def test_评审模型可与生成模型不同():
    llm = StubLLM([ANALYST_REPLY, _generator_reply(_case()), _review_reply(True)])
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
