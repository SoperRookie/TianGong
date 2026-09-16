"""知识库数据模型（F-7-1/3/4）：三大分类 + 知识空间隔离。"""

from pydantic import BaseModel, Field

# 三大知识库分类（PRD 固定枚举，注入配额 5:3:2 按此分类计算）
CATEGORIES: dict[str, str] = {
    "test_cases": "测试用例库",
    "requirement_docs": "需求文档库",
    "business_rules": "玩法与业务规则库",
}

DEFAULT_SPACE = "default"   # 历史空间：视同公共知识（迁移期兼容）
PUBLIC_SPACE = "public"     # 公共知识库（完整需求 16 章 / 核心规则 24）：显式标记方可跨项目使用
LEVELS = {"public": "公共知识库", "project": "项目知识库", "module": "模块知识库"}


class InvalidCategoryError(ValueError):
    def __init__(self, category: str):
        super().__init__(f"未知知识分类: {category}，可用: {sorted(CATEGORIES)}")


class KnowledgeDoc(BaseModel):
    """已入库的知识文档元数据（切片与向量存于向量库，此处只记台账）。"""

    doc_id: str
    space: str = Field(description="知识空间：项目名（项目/模块层）或 public（公共层）；历史 default 视同公共")
    level: str = Field(default="project", description="分层：public / project / module")
    module: str = Field(default="", description="模块层：模块路径（如 登录/密码找回）")
    created_by: str | None = Field(default=None)
    category: str = Field(description="三大分类之一")
    source: str = Field(description="来源文件名或 'text'")
    chunk_count: int
    embedding_model: str = Field(description="入库时使用的 Embedding 模型标识，换模型需重建向量")
    created_at: str


class SearchHit(BaseModel):
    """单条检索命中：text 为切片原文，score 为相似度（越大越相关）。"""

    text: str
    score: float
    doc_id: str
    source: str
    category: str
    space: str
    level: str = "project"
    module: str = ""
    chunk_index: int
