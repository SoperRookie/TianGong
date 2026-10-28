"""离线终稿覆盖（F-6-8 延伸）：回传的人工定稿文件直接成为线上用例，整份标为人工定稿。

之前回传只做字段级 diff 留痕，线上用例一条不改；用户导出 → 离线逐条改完 → 回传，期望的是「以我上传的为准、并且通过评审」。
配对规则（编号离线常被重排、标题也可能被改，两者都不能单独信）：
1. 编号 + 标题都一样；2. 标题一样（同题多条按顺序）；3. 编号一样（标题改了）。
配上的沿用原 uid（审核状态、版本链、计划引用都跟 uid），内容变了版本 +1、来源「评审通过终稿」；
文件里没有的进回收站（可恢复）；文件里多出来的新增（uid 新发、来源 import）。顺序按文件。
"""

from __future__ import annotations

from collections import defaultdict

from app.tasks.diff import _norm_title

# 文件里带的列覆盖线上的；没在文件里的（uid / point_ids / extras / source …）沿用线上的
_FILE_FIELDS = ("module", "title", "priority", "precondition", "steps", "keywords", "remark")
_CONTENT_SKIP = ("uid", "version", "case_id")


def _content(case: dict) -> dict:
    return {k: v for k, v in case.items() if k not in _CONTENT_SKIP}


def match_final(existing: list[dict], final: list[dict]) -> list[tuple[dict, dict | None]]:
    """每条终稿用例配到的线上用例（None = 新增）；按文件顺序。"""
    by_both: dict[tuple[str, str], list[dict]] = defaultdict(list)
    by_title: dict[str, list[dict]] = defaultdict(list)
    by_id: dict[str, list[dict]] = defaultdict(list)
    for c in existing:
        by_both[(str(c.get("case_id") or ""), _norm_title(c))].append(c)
        by_title[_norm_title(c)].append(c)
        by_id[str(c.get("case_id") or "")].append(c)
    taken: set[int] = set()

    def _take(pool: list[dict] | None) -> dict | None:
        for c in pool or []:
            if id(c) not in taken:
                taken.add(id(c))
                return c
        return None

    pairs: list[tuple[dict, dict | None]] = []
    rest: list[tuple[int, dict]] = []
    for i, f in enumerate(final):   # 第一轮只配「编号 + 标题」与「标题」，避免编号被重排后抢走别人的
        origin = _take(by_both.get((str(f.get("case_id") or ""), _norm_title(f)))) or _take(by_title.get(_norm_title(f)))
        pairs.append((f, origin))
        if origin is None:
            rest.append((i, f))
    for i, f in rest:                # 第二轮：标题改了的按编号配
        cid = str(f.get("case_id") or "")
        if cid:
            pairs[i] = (f, _take(by_id.get(cid)))
    return pairs


def apply_final(existing: list[dict], final: list[dict], new_uid) -> dict:
    """算出覆盖后的用例列表与变化清单：{cases, modified: [(uid, before, after)], added: [case], deleted: [case], invalid: [str]}。
    不落库、不记版本，由路由在校验全部通过后统一执行。"""
    from app.templates import TestCase

    pairs = match_final(existing, final)
    matched_ids = {id(o) for _, o in pairs if o is not None}
    out: list[dict] = []
    modified: list[tuple[str, dict, dict]] = []
    added: list[dict] = []
    invalid: list[str] = []
    for n, (f, origin) in enumerate(pairs, 1):
        patch = {k: f[k] for k in _FILE_FIELDS if k in f}
        base = dict(origin) if origin is not None else {"uid": new_uid(), "source": "import", "version": 1,
                                                        "case_id": str(f.get("case_id") or "") or "TC-000"}
        merged = {**base, **patch}
        if not str(merged.get("module") or "").strip():
            merged["module"] = base.get("module") or "未分组"
        try:
            case = TestCase.model_validate(merged).model_dump()
        except Exception as e:
            invalid.append(f"第 {n} 条「{_norm_title(f) or '（无标题）'}」：{str(e).splitlines()[0][:120]}")
            continue
        if origin is None:
            added.append(case)
        elif _content(case) != _content(origin):
            case["version"] = int(origin.get("version", 1) or 1) + 1
            modified.append((case["uid"], origin, case))
        out.append(case)
    deleted = [c for c in existing if id(c) not in matched_ids]
    return {"cases": out, "modified": modified, "added": added, "deleted": deleted, "invalid": invalid}
