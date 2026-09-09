"""项目依赖关系（项目知识库的一部分）：需求间的功能依赖、用例间的依赖链路。

- 严格项目隔离：一个项目一份文档（DocStore "dependencies"），边的两端必须都是本项目的需求 / 用例，
  跨项目节点一律拒绝；接口按项目鉴权（knowledge.view / knowledge.manage）。
- 两张图：
  * requirement：节点 = 需求实体，关系 depends（from 依赖 to：to 的功能是 from 的前置）/ related（关联）。
  * case：节点 = 正式用例（uid），关系 precondition（执行 from 前须先执行 to）/ data（数据依赖）/ related。
- 边来源：manual（人工，直接生效）、ai（AI 识别，先为 proposed 草稿，人工确认后生效）——遵守「AI 产物默认草稿」。
- depends / precondition 构成有向图，禁止成环（前置关系有环即死锁）；图接口返回分层（拓扑层级）与链路（最长前置链）。
"""

from __future__ import annotations

import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone

REQ_RELATIONS = {"depends": "功能依赖（前置）", "related": "关联"}
CASE_RELATIONS = {"precondition": "前置用例", "data": "数据依赖", "related": "关联"}
ORDERED = {"depends", "precondition"}   # 参与有向链路 / 成环校验的关系
KINDS = {"requirement": REQ_RELATIONS, "case": CASE_RELATIONS}


class DependencyError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DependencyStore:
    def __init__(self):
        from app.db import DocStore

        self._doc = DocStore("dependencies")
        self._docs: dict[str, dict] = self._doc.load_all()

    def _project_doc(self, project: str) -> dict:
        doc = self._docs.get(project)
        if doc is None:
            doc = {"project": project, "requirement": [], "case": [], "updated_at": _now()}
            self._docs[project] = doc
        return doc

    def _persist(self, project: str) -> None:
        doc = self._docs[project]
        doc["updated_at"] = _now()
        self._doc.put(project, doc)

    def edges(self, project: str, kind: str) -> list[dict]:
        return list(self._project_doc(project).get(kind) or [])

    def add(self, project: str, kind: str, src: str, dst: str, relation: str, note: str = "",
            by: str | None = None, source: str = "manual", reason: str = "") -> dict:
        if kind not in KINDS:
            raise DependencyError(f"未知依赖图: {kind}")
        if relation not in KINDS[kind]:
            raise DependencyError(f"未知关系类型: {relation}（可用 {'/'.join(KINDS[kind])}）")
        if not src or not dst:
            raise DependencyError("依赖两端不能为空")
        if src == dst:
            raise DependencyError("不能依赖自身")
        edges = self._project_doc(project)[kind]
        for e in edges:
            if e["from"] == src and e["to"] == dst and e["relation"] == relation:
                raise DependencyError("该依赖已存在")
        status = "confirmed" if source == "manual" else "proposed"
        if relation in ORDERED and status == "confirmed":
            self._check_cycle(edges, src, dst)
        edge = {"edge_id": uuid.uuid4().hex[:8], "from": src, "to": dst, "relation": relation,
                "note": (note or "").strip()[:300], "reason": (reason or "").strip()[:300],
                "source": source, "status": status, "created_by": by, "created_at": _now(),
                "confirmed_by": by if status == "confirmed" else None}
        edges.append(edge)
        self._persist(project)
        return edge

    def confirm(self, project: str, kind: str, edge_id: str, by: str | None = None) -> dict:
        edges = self._project_doc(project)[kind]
        edge = next((e for e in edges if e["edge_id"] == edge_id), None)
        if edge is None:
            raise DependencyError(f"依赖不存在: {edge_id}")
        if edge["status"] == "confirmed":
            return edge
        if edge["relation"] in ORDERED:
            self._check_cycle(edges, edge["from"], edge["to"])
        edge.update(status="confirmed", confirmed_by=by, confirmed_at=_now())
        self._persist(project)
        return edge

    def remove(self, project: str, kind: str, edge_id: str) -> dict:
        edges = self._project_doc(project)[kind]
        edge = next((e for e in edges if e["edge_id"] == edge_id), None)
        if edge is None:
            raise DependencyError(f"依赖不存在: {edge_id}")
        edges.remove(edge)
        self._persist(project)
        return edge

    def prune(self, project: str, kind: str, alive: set[str]) -> int:
        """节点（需求/用例）被删除后清理悬空边。"""
        edges = self._project_doc(project)[kind]
        keep = [e for e in edges if e["from"] in alive and e["to"] in alive]
        removed = len(edges) - len(keep)
        if removed:
            self._project_doc(project)[kind] = keep
            self._persist(project)
        return removed

    @staticmethod
    def _check_cycle(edges: list[dict], src: str, dst: str) -> None:
        """新增 src→dst 后若 dst 能回到 src 即成环（只看已生效的有序关系）。"""
        graph: dict[str, list[str]] = defaultdict(list)
        for e in edges:
            if e["relation"] in ORDERED and e["status"] == "confirmed":
                graph[e["from"]].append(e["to"])
        seen, stack = set(), [dst]
        while stack:
            node = stack.pop()
            if node == src:
                raise DependencyError("会形成循环依赖（前置关系不能成环）")
            if node in seen:
                continue
            seen.add(node)
            stack.extend(graph.get(node, []))


# ---- 图分析：分层、链路、环 ----


def analyze_graph(nodes: dict[str, dict], edges: list[dict]) -> dict:
    """对已生效的有序关系做拓扑分层（前置在上游）、最长链路与成环检测。

    返回 levels: {node_id: 层级}（0 = 无前置）、chains: 最长前置链（从最上游到最下游，节点 id 列表，最多 30 条）、
    cycles: 成环节点集合（数据被外部改动后仍可能出现，用于告警）。
    """
    ordered = [e for e in edges if e["relation"] in ORDERED and e["status"] == "confirmed"
               and e["from"] in nodes and e["to"] in nodes]
    # from 依赖 to：to 是上游。按 to → from 方向建拓扑（上游先）
    down: dict[str, list[str]] = defaultdict(list)   # 上游 → 下游
    indeg: dict[str, int] = defaultdict(int)
    involved: set[str] = set()
    for e in ordered:
        down[e["to"]].append(e["from"])
        indeg[e["from"]] += 1
        involved.update((e["from"], e["to"]))
    levels: dict[str, int] = {n: 0 for n in involved}
    queue = deque(sorted(n for n in involved if indeg[n] == 0))
    order: list[str] = []
    while queue:
        n = queue.popleft()
        order.append(n)
        for m in down[n]:
            levels[m] = max(levels[m], levels[n] + 1)
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    cycles = sorted(n for n in involved if n not in set(order))
    # 最长链路：从每个源点做 DFS 到汇点，取每个汇点的最长路径
    best: dict[str, list[str]] = {}
    ordered_set = set(order)

    def dfs(n: str, path: list[str]) -> None:
        if len(path) > 60:
            return
        outs = [m for m in down[n] if m in ordered_set]
        if not outs:
            if len(path) > len(best.get(n, [])):
                best[n] = list(path)
            return
        for m in outs:
            if m not in path:
                dfs(m, path + [m])

    for s in order:
        if all(e["from"] != s for e in ordered):   # 源点：不依赖任何节点
            dfs(s, [s])
    chains = sorted((c for c in best.values() if len(c) > 1), key=lambda c: (-len(c), c))[:30]
    return {"levels": levels, "chains": chains, "cycles": cycles, "involved": sorted(involved)}


def requirement_hints(requirements: list[dict], edges: list[dict]) -> list[dict]:
    """免 AI 的启发式建议：需求分析「外部依赖」文本命中本项目其他需求标题 → 提示可建 depends 边（不落库）。"""
    existing = {(e["from"], e["to"]) for e in edges}
    hints = []
    for r in requirements:
        deps = ((r.get("analysis") or {}).get("dependencies") or [])
        for text in deps:
            for other in requirements:
                if other["req_id"] == r["req_id"] or (r["req_id"], other["req_id"]) in existing:
                    continue
                title = (other.get("title") or "").strip()
                if len(title) >= 2 and title in str(text):
                    hints.append({"from": r["req_id"], "to": other["req_id"], "relation": "depends",
                                  "reason": f"需求分析·外部依赖：{str(text)[:80]}"})
    return hints


def build_infer_input(kind: str, nodes: list[dict]) -> str:
    """AI 识别依赖的输入：编号 + 标题 + 摘要，每行一个节点。"""
    lines = []
    for n in nodes:
        if kind == "requirement":
            summary = "；".join((n.get("features") or [])[:6])
            deps = "；".join((n.get("dependencies") or [])[:4])
            lines.append(f"[{n['id']}] {n['title']}" + (f" | 功能点：{summary}" if summary else "")
                         + (f" | 外部依赖：{deps}" if deps else ""))
        else:
            pre = (n.get("precondition") or "").strip().replace("\n", " ")
            lines.append(f"[{n['id']}] {n.get('case_id', '')} {n['title']} | 模块：{n.get('module', '')}"
                         + (f" | 前置条件：{pre[:120]}" if pre else ""))
    return "\n".join(lines)
