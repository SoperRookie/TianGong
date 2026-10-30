"""端（终端 / 系统面）的定义与判定：H5 / 游戏后台管理 / 商户后台 / 通用 / 端到端 / 其他。

一份需求通常同时写了几个端（H5 用户侧、活动运营的游戏后台、商户管理后台），提取完要按端汇总看总需求，拆解出的测试点要按端
分列表审核、按端单独生成用例、按端单独导出。端的判定分三层：
1. 需求提取时模型按整理稿的顶层章节（文件夹 / 页面）判一次（Prompt `terminal_classify`），人可在提取页改；
2. 任务拆解时模块名按整理稿章节对回章节的端（模块名就是「文件夹-页面-区块」）；
3. 都对不上的按名字关键词兜底，再兜不住的记「其他」。
端的清单可用 config/terminals.yaml 覆盖（key / label / keywords），没有就用内置三端。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

E2E = "e2e"
COMMON = "common"
OTHER = "other"

_BUILTIN: list[dict] = [
    {"key": "h5", "label": "H5",
     "keywords": ["h5", "前台", "用户端", "用户侧", "玩家端", "玩家侧", "会员端", "移动端", "手机端", "app", "小程序", "客户端", "游戏端",
                  "游戏大厅", "大厅", "活动页", "活动入口", "c端", "前端页面", "web端", "pc端"]},
    {"key": "game_admin", "label": "游戏后台管理",
     "keywords": ["游戏后台", "活动运营后台", "运营后台", "总后台", "平台后台", "游戏管理", "运营管理", "活动后台", "后台管理", "管理后台",
                  "活动配置", "活动设置管理", "派奖", "风控后台"]},
    {"key": "merchant_admin", "label": "商户后台",
     "keywords": ["商户后台", "商户管理后台", "商户端", "商户管理", "商户侧", "商家后台", "代理后台", "租户后台", "商户报表", "会员管理/商户管理"]},
]
_FIXED: list[dict] = [
    {"key": COMMON, "label": "通用",
     "keywords": ["需求说明", "需求背景", "需求概述", "版本记录", "更新记录", "修订记录", "变更记录", "功能清单", "术语", "总体说明", "概述", "通用规则", "公共规则"]},
    {"key": E2E, "label": "端到端", "keywords": ["端到端"]},
    {"key": OTHER, "label": "其他", "keywords": []},
]

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "terminals.yaml"


@lru_cache(maxsize=1)
def terminals() -> list[dict]:
    """端的清单（业务端在前、通用 / 端到端 / 其他固定在后）。config/terminals.yaml 存在且合法时覆盖业务端。"""
    items = _BUILTIN
    if _CONFIG_PATH.exists():
        try:
            import yaml

            raw = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
            got = raw.get("terminals") if isinstance(raw, dict) else raw
            parsed = []
            for t in got or []:
                if not isinstance(t, dict) or not str(t.get("key") or "").strip():
                    continue
                key = str(t["key"]).strip()
                if key in (COMMON, E2E, OTHER):
                    continue
                parsed.append({"key": key, "label": str(t.get("label") or key).strip(),
                               "keywords": [str(k).strip() for k in (t.get("keywords") or []) if str(k).strip()]})
            if parsed:
                items = parsed
        except Exception:  # 配置写坏了退回内置，不影响启动
            items = _BUILTIN
    return [dict(t) for t in items] + [dict(t) for t in _FIXED]


def business_terminals() -> list[dict]:
    """要人审核 / 生成 / 导出的业务端（不含通用 / 端到端 / 其他）。"""
    return [t for t in terminals() if t["key"] not in (COMMON, E2E, OTHER)]


def terminal_keys() -> list[str]:
    return [t["key"] for t in terminals()]


def label_of(key: str | None) -> str:
    key = key or OTHER
    return next((t["label"] for t in terminals() if t["key"] == key), key)


def valid_key(key: str | None) -> str | None:
    key = (key or "").strip()
    return key if key in terminal_keys() else None


_PRIORITY = ("merchant_admin", "game_admin")   # 「商户管理后台」含「管理后台」、「游戏后台-大厅配置」含「大厅」：后台类先判


def classify_name(text: str) -> str | None:
    """按名字关键词判端：商户后台 › 游戏后台 › H5 › 通用（「商户管理后台」含「管理后台」，先判商户）。
    对不上返回 None（调用方决定兜底到哪）。匹配不分大小写；名字里的「/ - ›」分段只是分隔符。"""
    s = (text or "").strip().lower()
    if not s:
        return None
    allt = terminals()
    ordered = [t for k in _PRIORITY for t in allt if t["key"] == k] + \
              [t for t in allt if t["key"] not in (*_PRIORITY, COMMON, E2E, OTHER)] + \
              [t for t in allt if t["key"] == COMMON]
    for t in ordered:
        for kw in t["keywords"]:
            k = kw.lower()
            if not k:
                continue
            if k.isascii() and k.isalnum():
                if re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", s):
                    return t["key"]
            elif k in s:
                return t["key"]
    return None


# ---- 整理稿：顶层章节 → 端 ----


def section_key(sec: dict) -> str:
    """整理稿顶层章节的键：有文件夹分组的用文件夹名，否则页面名（与 outline_from_headings 生成的模块名前缀一致）。"""
    return str(sec.get("group") or sec.get("title") or "").strip()


def top_sections(outline: list[dict]) -> list[dict]:
    """把页面级大纲按顶层章节归并：[{key, no, title, group, pages: [{no, title, blocks, items}], blocks, items}]。"""
    out: list[dict] = []
    index: dict[str, dict] = {}
    for sec in outline or []:
        key = section_key(sec)
        if not key:
            continue
        blocks = sec.get("blocks") or []
        items = sum(_block_items(b) for b in blocks)
        top = index.get(key)
        if top is None:
            top = {"key": key, "no": str(sec.get("gno") or sec.get("no") or ""), "title": key, "group": bool(sec.get("group")),
                   "pages": [], "blocks": 0, "items": 0}
            index[key] = top
            out.append(top)
        top["pages"].append({"no": str(sec.get("no") or ""), "title": str(sec.get("title") or ""), "blocks": len(blocks), "items": items})
        top["blocks"] += len(blocks)
        top["items"] += items
    return out


def _block_items(block: dict) -> int:
    """区块里的条目数：按行类型算（表格 / 流程行按 ids，示例图按说明 + 文案，其余一行一条），接口层级与成稿层级两种行结构都认。"""
    n = 0
    for r in block.get("rows") or []:
        if not isinstance(r, dict):
            continue
        t = r.get("t")
        many = r.get("ids") if r.get("ids") is not None else r.get("items")
        if t in ("table", "flow"):
            n += len(many or [])
        elif t == "example":
            n += 1 + len(many or [])
        else:
            n += 1
    return n


def section_terminals(outline: list[dict], overrides: dict | None = None) -> dict[str, str]:
    """每个顶层章节的端：先看模型 / 人工给的 overrides（章节键 → 端），没有的按关键词判，再没有的记「其他」。"""
    overrides = overrides or {}
    out: dict[str, str] = {}
    for top in top_sections(outline):
        key = top["key"]
        t = valid_key(overrides.get(key))
        if t is None:
            t = classify_name(key) or next((classify_name(p["title"]) for p in top["pages"] if classify_name(p["title"])), None) or OTHER
        out[key] = t
    return out


def terminal_summary(outline: list[dict], overrides: dict | None = None) -> dict:
    """三端汇总：每个端有哪些章节 / 页面、多少区块与条目，以及总计。只列有内容的端，顺序按端清单。"""
    tops = top_sections(outline)
    terms = section_terminals(outline, overrides)
    groups: dict[str, list[dict]] = {}
    for top in tops:
        groups.setdefault(terms.get(top["key"], OTHER), []).append(top)
    rows = []
    for t in terminals():
        secs = groups.get(t["key"]) or []
        if not secs:
            continue
        rows.append({"key": t["key"], "label": t["label"], "sections": [dict(s, terminal=t["key"]) for s in secs],
                     "pages": sum(len(s["pages"]) for s in secs), "blocks": sum(s["blocks"] for s in secs),
                     "items": sum(s["items"] for s in secs)})
    total = {"sections": len(tops), "pages": sum(len(s["pages"]) for s in tops),
             "blocks": sum(s["blocks"] for s in tops), "items": sum(s["items"] for s in tops)}
    return {"terminals": rows, "total": total}


def extraction_section_terminals(extraction: dict | None) -> dict[str, str]:
    """一份整理稿（有逐条结构）的章节 → 端；没有逐条结构返回空。"""
    ex = extraction or {}
    if not ex.get("modules"):
        return {}
    from app.agents.extraction import extraction_outline

    return section_terminals(extraction_outline(ex["modules"]), ex.get("terminals") or {})


# ---- 任务：模块 / 测试点 / 用例 → 端 ----


def terminal_of_module(name: str, sections: dict[str, str] | None = None, titles: list[str] | None = None) -> str:
    """模块名对端：先去掉多需求合并时的「需求标题-」前缀，再按整理稿章节键最长前缀匹配（「活动运营后台-活动列表-基本配置」对「活动运营后台」），
    对不上按关键词，再对不上「其他」。"""
    name = (name or "").strip()
    bare = name
    for title in titles or []:
        if title and name.startswith(f"{title}-"):
            bare = name[len(title) + 1:]
            break
    best = ""
    for key, term in (sections or {}).items():
        if not key:
            continue
        for cand in (bare, name):
            if (cand == key or cand.startswith(f"{key}-")) and len(key) > len(best):
                best = key
    if best:
        return sections[best]
    return classify_name(bare) or classify_name(name) or OTHER


def fill_point_terminals(analysis: dict | None, force: bool = False) -> int:
    """给拆解结果里没有端的测试点补上端（幂等）；端到端场景点固定「端到端」。返回补了几条。
    规则来自 analysis["terminal_rules"] = {sections: 章节 → 端, titles: 多需求标题}（任务从需求发起时写入；上传路径为空只按关键词）。"""
    if not analysis:
        return 0
    rules = analysis.get("terminal_rules") or {}
    sections, titles = rules.get("sections") or {}, rules.get("titles") or []
    n = 0
    for entry in analysis.get("test_points") or []:
        module = str(entry.get("module") or "")
        mod_term: str | None = None
        for p in entry.get("points") or []:
            if not isinstance(p, dict):
                continue
            if p.get("source") == E2E:
                want = E2E
            else:
                if mod_term is None:
                    mod_term = terminal_of_module(module, sections, titles)
                want = mod_term
            if force or not valid_key(p.get("terminal")):
                if p.get("terminal") != want:
                    n += 1
                p["terminal"] = want
    return n


def point_terminal(p: dict) -> str:
    return valid_key(p.get("terminal")) or (E2E if p.get("source") == E2E else OTHER)


def module_terminals(modules: list[dict]) -> dict[str, str]:
    """每个模块的端：取模块内测试点的多数端（人工把个别点改到别的端不影响模块归属）。"""
    out: dict[str, str] = {}
    for entry in modules or []:
        counts: dict[str, int] = {}
        for p in entry.get("points") or []:
            if isinstance(p, dict):
                t = point_terminal(p)
                counts[t] = counts.get(t, 0) + 1
        if counts:
            out[str(entry.get("module") or "")] = max(counts, key=lambda k: (counts[k], -terminal_keys().index(k)))
    return out


def filter_modules(modules: list[dict], terminal: str | None) -> list[dict]:
    """只留某个端的测试点（模块结构不变，空模块去掉）；terminal 为空返回原列表。"""
    if not terminal:
        return modules
    out = []
    for entry in modules or []:
        pts = [p for p in entry.get("points") or [] if isinstance(p, dict) and point_terminal(p) == terminal]
        if pts:
            out.append({**entry, "points": pts})
    return out


def point_terminal_stats(modules: list[dict], generated: set[str] | None = None) -> list[dict]:
    """按端统计测试点：总数 / 待审核 / 已通过 / 已驳回 / 已生成用例 / 已通过但未生成。只列有测试点的端。"""
    generated = generated or set()
    stats: dict[str, dict] = {}
    for entry in modules or []:
        for p in entry.get("points") or []:
            if not isinstance(p, dict):
                continue
            t = point_terminal(p)
            s = stats.setdefault(t, {"key": t, "label": label_of(t), "total": 0, "pending": 0, "approved": 0, "rejected": 0,
                                     "generated": 0, "pending_generation": 0, "modules": 0})
            s["total"] += 1
            st = p.get("status") or "pending"
            s[st if st in ("approved", "rejected") else "pending"] += 1
            tp = str(p.get("tp_id") or "")
            if tp and tp in generated:
                s["generated"] += 1
            elif st == "approved":
                s["pending_generation"] += 1
    for entry in modules or []:
        seen = {point_terminal(p) for p in entry.get("points") or [] if isinstance(p, dict)}
        for t in seen:
            if t in stats:
                stats[t]["modules"] += 1
    return [stats[k] for k in terminal_keys() if k in stats]


def case_terminal(case: dict, point_terms: dict[str, str], module_terms: dict[str, str]) -> str:
    """用例的端：来源测试点的多数端 › 所属模块的端 › 模块名关键词 › 其他。"""
    counts: dict[str, int] = {}
    for tp in case.get("point_ids") or []:
        t = point_terms.get(str(tp))
        if t:
            counts[t] = counts.get(t, 0) + 1
    if counts:
        return max(counts, key=lambda k: (counts[k], -terminal_keys().index(k)))
    module = str(case.get("module") or "")
    if module in module_terms:
        return module_terms[module]
    return classify_name(module) or OTHER


def case_terminals(record) -> dict[str, str]:
    """任务里每条用例的端 {uid: 端}。"""
    modules = (record.analysis or {}).get("test_points") or []
    point_terms = {str(p.get("tp_id")): point_terminal(p) for e in modules for p in e.get("points") or []
                   if isinstance(p, dict) and p.get("tp_id")}
    mod_terms = module_terminals(modules)
    out: dict[str, str] = {}
    for c in (record.result or {}).get("cases") or []:
        uid = str(c.get("uid") or "")
        if uid:
            out[uid] = case_terminal(c, point_terms, mod_terms)
    return out
