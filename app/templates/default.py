"""内置默认用例模板（F-4-2）与用例数据模型。

优先级已确认为 4 级 P0-P3（2026-07-10 决策）：
P0=冒烟/核心链路，P1=主功能正常流与重要异常流，P2=次要功能与边界，P3=极端场景/体验/建议项。
测试步骤与预期结果为一一对应的父子结构（与团队 XMind 模板一致）。
"""

from typing import ClassVar, Literal

from pydantic import BaseModel, Field, field_validator

Priority = Literal["P0", "P1", "P2", "P3"]


class TestStep(BaseModel):
    action: str = Field(description="测试步骤")
    expected: str = Field(description="该步骤的预期结果")

    @field_validator("action", "expected")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("测试步骤与预期结果不能为空")
        return v.strip()


class TestCase(BaseModel):
    __test__: ClassVar[bool] = False  # 避免 pytest 将其误收集为测试类

    case_id: str = Field(description="用例编号，如 TC-登录-001")
    module: str = Field(description="所属模块")
    title: str = Field(description="用例标题")
    priority: Priority
    precondition: str = Field(default="", description="前置条件")
    steps: list[TestStep] = Field(min_length=1, description="步骤与预期结果一一对应")
    keywords: str = Field(default="", description="关键词，逗号/顿号分隔（需求六十输出格式）")
    remark: str = Field(default="", description="备注")
    extras: dict[str, str] = Field(default_factory=dict, description="自定义模板扩展字段：列名 -> 值")
    uid: str = Field(default="", description="系统内部稳定标识：审核状态跟随 uid，不受编号重排影响")
    version: int = Field(default=1, description="乐观锁版本号（完整需求 11 章）：内容每次修改 +1，保存时比对拦截并发覆盖")

    @field_validator("case_id", "module", "title")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("字段不能为空")
        return v.strip()


class CaseTemplate(BaseModel):
    """模板定义：字段规范同时用于 Prompt 约束与结果校验（F-4-5 的基础）。"""

    name: str
    field_specs: dict[str, str] = Field(description="字段名 -> 填写规范，注入生成 Prompt")

    def prompt_spec(self) -> str:
        return "\n".join(f"- {field}: {spec}" for field, spec in self.field_specs.items())


DEFAULT_TEMPLATE = CaseTemplate(
    name="内置默认模板",
    field_specs={
        "case_id": "用例编号，格式 TC-<模块>-<三位序号>，同一模块内连续递增，如 TC-登录-001",
        "module": "所属功能模块，与测试点拆解的模块名一致",
        "title": "用例标题，一句话说明验证目标，动宾结构，不超过 40 字",
        "priority": "优先级，仅允许 P0/P1/P2/P3：P0=冒烟与核心链路；P1=主功能正常流与重要异常流；P2=次要功能与边界场景；P3=极端场景、体验类、建议项",
        "precondition": "前置条件，描述执行前系统与数据状态，无则留空",
        "steps": "测试步骤数组，每步含 action（操作）与 expected（该步预期结果），一一对应，单条用例不超过 8 步",
        "keywords": "关键词，3-6 个概括测试对象与场景的词，顿号分隔，如「登录、密码错误、账号锁定」",
        "remark": "备注，可为空",
    },
)
