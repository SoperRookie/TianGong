"""生成质量闭环 Agent（生成质量核心需求设计）：

- 独立查漏 Agent + 覆盖矩阵（需求七/八/九/十）：与生成分离的第二轮检查，只允许新增；
- 重复测试点语义复核（需求十一~十三）：文字初筛候选交 LLM 复核，人工决策处置；
- 测试点/用例驳回定点修改（需求三十~三十三）：只输入被驳回对象+审核意见，
  锁定资产确定性保护（模型无法越权修改），输出修改前后 Diff（需求三十四）；
- 需求变更差异分析（需求四十~四十五）：识别变化类型与受影响资产，最小范围更新；
- 学习候选提炼（需求三十六~三十八）：从人工修改留痕归纳规则候选，人工确认后生效。
"""

import json
import re

from loguru import logger
from pydantic import ValidationError

from app.agents.graph import _chat_json
from app.agents.context import profile_block
from app.agents.prompts import wrap_data
from app.prompts import prompt_text
from app.llm.client import LLMClient
from app.tasks.points import (
    COVERAGE_STATES,
    DIMENSIONS,
    add_points,
    clear_reject_fields,
    coarse_warnings,
    find_point,
    iter_points,
    new_uid,
)
from app.templates import CustomTemplate, TestCase, builtin_default_template


def _dump(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1)


def _points_view(modules: list[dict]) -> list[dict]:
    """测试点的精简视图（注入 Prompt）：只带 tp_id / 内容 / 维度，不泄漏审核状态字段。"""
    return [
        {
            "module": e.get("module", ""),
            "points": [
                {"tp_id": p.get("tp_id", ""), "point": p.get("point", ""), "dimension": p.get("dimension", ""),
                 **({"basis": p["basis"]} if p.get("basis") else {})}
                for p in e.get("points", [])
            ],
        }
        for e in modules
    ]


# ---- 独立查漏（需求七/八/十）----

# 查漏按章节分批：每批送这些字的需求原文与这些章节的测试点，让模型逐句核对。线上 7.6 万字需求加 1255 条测试点
# 一次送进去（10.6 万 token），模型 9 秒就回「十个维度全已覆盖、新增 0 条」，整轮等于没跑。
GAP_BATCH_CHARS = 8000
# 对不到任何批的测试点占比超过这个数（模型自拟模块名、需求不是整理稿格式）就不分批，整篇一次查
GAP_UNMATCHED_LIMIT = 0.5


def _coverage(data: dict) -> dict:
    coverage = {}
    raw = data.get("coverage", {}) if isinstance(data.get("coverage"), dict) else {}
    for dim in DIMENSIONS:
        value = str(raw.get(dim, "待确认")).strip()
        coverage[dim] = value if value in COVERAGE_STATES else "待确认"
    return coverage


def _additions(data: dict) -> list[dict]:
    return [a for a in data.get("additions", []) if isinstance(a, dict) and str(a.get("point", "")).strip()]


def merge_coverage(parts: list[dict]) -> dict:
    """几批的覆盖矩阵合成一份：任一批未覆盖即未覆盖；否则有已覆盖即已覆盖；全不适用才不适用；其余待确认。"""
    out = {}
    for dim in DIMENSIONS:
        values = [p.get(dim, "待确认") for p in parts]
        if "未覆盖" in values:
            out[dim] = "未覆盖"
        elif "已覆盖" in values:
            out[dim] = "已覆盖"
        elif values and all(v == "不适用" for v in values):
            out[dim] = "不适用"
        else:
            out[dim] = "待确认"
    return out


def apply_additions_to_coverage(coverage: dict, additions: list[dict], parts: list[dict] | None = None) -> tuple[dict, dict]:
    """查漏补完再定矩阵：某维度有批次判「未覆盖」但 additions 里补了该维度的点，这一轮结束时它就是已覆盖（查漏补上的），
    矩阵改成「已覆盖」并在 notes 记「查漏补充 N 条」；补了 0 条才留「未覆盖」。线上 9 批合并后 7 个维度全「未覆盖」，
    而 303 条查漏新增恰恰就是补这些维度的——矩阵说的是补之前的状态，用户看到的却是补之后的列表。
    parts 给了时 notes 还记有几批判未覆盖（「3/9 批未覆盖」）。"""
    by_dim: dict[str, int] = {}
    for a in additions:
        dim = str(a.get("dimension") or "").strip()
        if dim:
            by_dim[dim] = by_dim.get(dim, 0) + 1
    out = dict(coverage)
    notes: dict[str, str] = {}
    for dim, status in coverage.items():
        n = by_dim.get(dim, 0)
        miss = sum(1 for p in (parts or []) if p.get(dim) == "未覆盖")
        bits = []
        if parts and miss:
            bits.append(f"{miss}/{len(parts)} 批判未覆盖")
        if n:
            bits.append(f"查漏补充 {n} 条")
            if status == "未覆盖":
                out[dim] = "已覆盖"
        if bits:
            notes[dim] = "，".join(bits)
    return out, notes


_REF_NUM_RE = re.compile(r"(\d+(?:\.\d+)+)")


def gap_batches(requirement: str, modules: list[dict], max_chars: int | None = None) -> list[tuple[str, list[dict]]]:
    """查漏分批：需求按章节切成若干批（与拆解同一套分片，确认结论也按模块跟到对应批），每批带与它章节相关的测试点——
    模块名对得上批里章节的（`match_module`）跟那一批；对不上的看测试点 ref 里的章节号落在哪一批；
    都对不上的每批都带（多花 token，不会让模型把别处已有的点当漏）。整篇不超一批、或对不上的点超过一半时只有一批、带全部测试点。"""
    from app.agents.outline import _HEAD_RE, clean_manual_sections, heading_modules, match_module, requirement_chunks

    requirement = clean_manual_sections(requirement)   # 与拆解看同一份文本：「人工收录」小节并入所在页面
    chunks, _ = requirement_chunks(requirement, max_chars or GAP_BATCH_CHARS)
    if len(chunks) <= 1:
        return [(requirement, modules)]
    heads = heading_modules(requirement)
    names: list[list[str]] = []
    nums: list[set[str]] = []
    for c in chunks:
        lines = c.splitlines()
        names.append([heads[l.strip()] for l in lines if l.strip() in heads])
        nums.append({m.group(2) for l in lines if (m := _HEAD_RE.match(l))})
    all_names = [n for ns in names for n in ns]
    per: list[list[dict]] = [[] for _ in chunks]
    unmatched = 0
    total = 0
    for entry in modules:
        points = entry.get("points", [])
        total += len(points)
        hit = match_module(str(entry.get("module", "")), all_names)
        where = {i for i, ns in enumerate(names) if any(n in ns for n in hit)}
        if len(where) != 1:   # 模块名对不上 / 跨批：按 ref 里的章节号投票
            votes = [0] * len(chunks)
            for p in points:
                m = _REF_NUM_RE.search(str(p.get("ref", "")))
                if m:
                    for i, ns in enumerate(nums):
                        if m.group(1) in ns:
                            votes[i] += 1
            where = {votes.index(max(votes))} if any(votes) else set()
        if len(where) == 1:
            per[where.pop()].append(entry)
        else:
            unmatched += len(points)
            for bucket in per:
                bucket.append(entry)
    if total and unmatched / total > GAP_UNMATCHED_LIMIT:
        return [(requirement, modules)]
    return list(zip(chunks, per))


async def run_gap_check(
    llm: LLMClient, requirement: str, modules: list[dict], model: str | None
) -> dict:
    """独立查漏 Agent：输出覆盖矩阵 + 新增建议；确定性保证存量测试点不被改动。
    长需求按章节分批并行查（见 gap_batches），各批覆盖矩阵合并、新增建议拼接；某批故障只丢该批。"""
    batches = gap_batches(requirement, modules)

    async def _one(no: int, text: str, mods: list[dict]) -> tuple[dict, list[dict], str]:
        scope = (f"本批是需求的第 {no}/{len(batches)} 批章节，只核对本批原文：逐句对照，凡是本批原文里的功能描述、业务规则、"
                 f"字段规则、数值约束没有任何测试点覆盖的，都在 additions 里补上（归入已有模块名、ref 指向本批原文）；"
                 f"coverage 只按本批章节判断。\n\n") if len(batches) > 1 else ""
        data, result = await _chat_json(
            llm,
            [
                {"role": "system", "content": prompt_text("gap_check")},
                {
                    "role": "user",
                    "content": f"{scope}需求内容：\n{wrap_data('需求原文', text)}\n\n已有测试点：\n{wrap_data('测试点', _dump(_points_view(mods)))}"
                               + profile_block(),
                },
            ],
            model,
        )
        return _coverage(data), _additions(data), result.model_name

    parts: list[dict] = []
    if len(batches) == 1:
        coverage, additions, model_name = await _one(1, *batches[0])
    else:
        import asyncio

        outcomes = await asyncio.gather(*[_one(i, t, m) for i, (t, m) in enumerate(batches, 1)], return_exceptions=True)
        done = [o for o in outcomes if not isinstance(o, BaseException)]
        for i, o in enumerate(outcomes, 1):
            if isinstance(o, BaseException):
                logger.warning("查漏第 {} 批失败（只丢该批）：{}", i, o)
        if not done:
            raise next(o for o in outcomes if isinstance(o, BaseException))
        parts = [c for c, _, _ in done]
        coverage = merge_coverage(parts)
        additions = [a for _, adds, _ in done for a in adds]
        model_name = done[0][2]
        logger.info("查漏按章节分 {} 批（每批 ≤ {} 字），{} 批完成", len(batches), GAP_BATCH_CHARS, len(done))
    coverage, notes = apply_additions_to_coverage(coverage, additions, parts or None)
    logger.info(
        "独立查漏完成：未覆盖 {} 项 / 新增建议 {} 条",
        sum(1 for v in coverage.values() if v == "未覆盖"), len(additions),
    )
    return {"coverage": coverage, "coverage_notes": notes, "additions": additions, "model_name": model_name, "batches": len(batches)}


# ---- 分片盲区复核：前面分片的盲区若被后面分片的需求章节说明，就不是盲区 ----


async def run_blind_spot_verify(
    llm: LLMClient, chunks: list[str], blind_spots: list[str], model: str | None
) -> list[dict]:
    """用各分片原文逐片复核盲区清单，返回已被原文说明的盲区 [{text, evidence, chunk}]。

    分片拆解时每片只看到自己那段需求，A 片标出的「未说明锁定时长」可能在 C 片写得清清楚楚。
    每片一次调用并行执行；任一片判定已说明即从盲区中剔除，并保留原文依据供人工核对。
    LLM 故障时按「无法复核」处理（返回空列表，盲区原样保留），不阻塞拆解。
    """
    spots = [b for b in blind_spots if str(b).strip()]
    if not spots or not chunks:
        return []
    listing = _dump([{"index": i, "blind_spot": b} for i, b in enumerate(spots, 1)])

    async def _one(no: int, chunk: str) -> list[dict]:
        try:
            data, _ = await _chat_json(
                llm,
                [
                    {"role": "system", "content": prompt_text("blind_spot_verify")},
                    {"role": "user", "content": f"需求原文（第 {no}/{len(chunks)} 片）：\n{wrap_data('需求原文', chunk)}\n\n"
                                                f"盲区清单：\n{wrap_data('盲区清单', listing)}"},
                ],
                model,
            )
        except Exception as e:  # noqa: BLE001 - 复核失败不阻塞拆解：盲区原样保留
            logger.warning("盲区复核第 {} 片失败（盲区原样保留）：{}", no, e)
            return []
        out = []
        for item in data.get("explained", []) if isinstance(data.get("explained"), list) else []:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("index"))
            except (TypeError, ValueError):
                continue
            if 1 <= idx <= len(spots):
                out.append({"text": spots[idx - 1], "evidence": str(item.get("evidence", "")).strip(), "chunk": no})
        return out

    import asyncio

    results = await asyncio.gather(*[_one(i, c) for i, c in enumerate(chunks, 1)])
    explained: dict[str, dict] = {}
    for item in (x for group in results for x in group):
        explained.setdefault(item["text"], item)  # 多片都说明了：记第一处依据
    if explained:
        logger.info("盲区复核：{} 条盲区已被需求其他分片说明，不再视为盲区", len(explained))
    return list(explained.values())


# ---- 测试点独立评审（拆解 / 延伸 / 查漏之后、人工审核之前）----

POINT_REVIEW_VERDICTS = ("合写", "不可验证", "数值改写", "ref 对不上", "UI 外观", "超出需求", "维度不符")
_QUOTE_VERDICTS = ("数值改写", "ref 对不上", "超出需求")   # 这三类意见要引原文，程序核验 quote 对不上就标「未核验」


def _review_view(modules: list[dict]) -> list[dict]:
    """送评审的测试点：只送待审核的（人已通过 / 驳回的不再评），带 ref 与 basis（评审要靳它们判 ref 对不上 / 超出需求）。"""
    out = []
    for e in modules:
        pts = [{"tp_id": p.get("tp_id", ""), "point": p.get("point", ""), "dimension": p.get("dimension", ""),
                **({"ref": p["ref"]} if p.get("ref") else {}), **({"basis": p["basis"]} if p.get("basis") else {})}
               for p in e.get("points", []) if isinstance(p, dict) and p.get("tp_id") and p.get("status", "pending") == "pending"]
        if pts:
            out.append({"module": e.get("module", ""), "points": pts})
    return out


async def run_point_review(llm: LLMClient, requirement: str, modules: list[dict], model: str | None) -> dict:
    """测试点独立评审：按章节分批（与查漏同一 `gap_batches`，每批带本批原文与本批章节的待审核测试点），并行调 Prompt
    `point_review`，逐条挑七类硬伤。程序兜底：只接受本批内的 tp_id 与枚举内的 verdict；数值改写 / ref 对不上 / 超出需求
    三类的 quote 用 `quote_found` 对本批原文核验，对不上的标 `unverified`；一条测试点只留第一条意见；某批故障只丢该批。
    返回 {"issues": [{tp_id, verdict, fix, note, quote, unverified?}], "reviewed": 送评条数, "batches": 批数, "failed": 故障批数}。"""
    from app.agents.analysis import normalize, quote_found

    batches = [(text, _review_view(mods)) for text, mods in gap_batches(requirement, modules)]
    batches = [(t, v) for t, v in batches if v]
    if not batches:
        return {"issues": [], "reviewed": 0, "batches": 0, "failed": 0}

    async def _one(no: int, text: str, view: list[dict]) -> list[dict]:
        scope = (f"本批是需求的第 {no}/{len(batches)} 批章节，测试点是从本批原文拆出来的，只对照本批原文评。\n\n"
                 if len(batches) > 1 else "")
        data, _ = await _chat_json(
            llm,
            [{"role": "system", "content": prompt_text("point_review")},
             {"role": "user", "content": f"{scope}需求原文：\n{wrap_data('需求原文', text)}\n\n待评审的测试点：\n{wrap_data('测试点', _dump(view))}"
                                         + profile_block()}],
            model,
        )
        allowed = {p["tp_id"] for e in view for p in e["points"]}
        src = normalize(text)
        out: list[dict] = []
        seen: set[str] = set()
        for item in (data.get("issues") or []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            tp, verdict = str(item.get("tp_id") or "").strip(), str(item.get("verdict") or "").strip()
            if tp not in allowed or tp in seen or verdict not in POINT_REVIEW_VERDICTS:
                continue
            seen.add(tp)
            quote = str(item.get("quote") or "").strip()
            issue = {"tp_id": tp, "verdict": verdict, "fix": str(item.get("fix") or "").strip(),
                     "note": str(item.get("note") or "").strip(), "quote": quote}
            if verdict in _QUOTE_VERDICTS and not (quote and quote_found(quote, src)):
                issue["unverified"] = True
            out.append(issue)
        return out

    import asyncio
    outcomes = await asyncio.gather(*[_one(i, t, v) for i, (t, v) in enumerate(batches, 1)], return_exceptions=True)
    issues: list[dict] = []
    failed = 0
    for i, o in enumerate(outcomes, 1):
        if isinstance(o, BaseException):
            failed += 1
            logger.warning("测试点评审第 {} 批失败（只丢该批）：{}", i, o)
        else:
            issues.extend(o)
    if failed == len(batches):
        raise next(o for o in outcomes if isinstance(o, BaseException))
    reviewed = sum(len(p) for _, v in batches for e in v for p in [e["points"]])
    logger.info("测试点独立评审：{} 条送评 / {} 批 / 意见 {} 条（未核验 {}）", reviewed, len(batches), len(issues),
                sum(1 for x in issues if x.get("unverified")))
    return {"issues": issues, "reviewed": reviewed, "batches": len(batches), "failed": failed}


# ---- 模块级组合延伸：单条规则各自成立不等于叠在一起也成立，在一个模块内找交叉场景 ----


COMBINE_MIN_POINTS = 3       # 少于这么多条（未驳回）测试点的模块没有组合的材料，不送
COMBINE_MAX_PER_MODULE = 8   # 每个模块最多收这么多条组合点（Prompt 同口径：模型按风险排序，截掉的是靠后的）
COMBINE_SIMILAR = 0.75       # 与本模块已有点或本轮前面的组合点相似度到这么多算复述，跳过
COMBINE_CONCURRENCY = 6      # 同时在跑的模块数（每个模块一次调用）
_COMBINE_LIST_KEYS = ("combinations", "extensions", "additions", "points", "组合", "测试点")
_COMBINE_FROM_KEYS = ("from", "combo_from", "sources", "tp_ids", "来源")


def _module_texts(requirement: str, modules: list[dict]) -> dict[str, str]:
    """每个模块所在章节的需求原文：复用查漏分批（模块名对章节、ref 章节号投票）；整篇一批、或模块落到多批（对不上）的用整篇。"""
    batches = gap_batches(requirement, modules)
    if len(batches) <= 1:
        text = batches[0][0] if batches else requirement
        return {str(e.get("module", "")): text for e in modules}
    hits: dict[str, list[str]] = {}
    for text, mods in batches:
        for e in mods:
            hits.setdefault(str(e.get("module", "")), []).append(text)
    full = "\n\n".join(t for t, _ in batches)
    return {name: (texts[0] if len(texts) == 1 else full) for name, texts in hits.items()}


async def run_point_combine(llm: LLMClient, requirement: str, modules: list[dict], model: str | None,
                            only: list[str] | None = None) -> dict:
    """模块级组合延伸（Prompt `point_combine`）：每个模块（未驳回测试点 ≥ COMBINE_MIN_POINTS，`only` 给了就只跑这些模块）各调一次，
    送该模块全部测试点（tp_id / point / dimension / ref / basis）+ 它所在章节的原文，按判定表 / 场景串联 / 状态×角色×端 /
    规则优先级 / 数据流转找单条规则叠加后的组合场景。程序兜底：模块固定为被处理的那个；`from` 只认本模块编号且 ≥ 2 条（不足的是单点
    场景不是组合）；没 basis 跳过；与本模块已有点相似 ≥ COMBINE_SIMILAR 跳过；每模块截到 COMBINE_MAX_PER_MODULE；某模块故障只丢该模块。
    返回 {"additions": [{module, point, dimension, ref, basis, combo_from}], "skipped": [{module, point, reason}], "modules": 送了几个模块,
    "failed": 故障模块数, "model_name"}。"""
    import asyncio

    from app.tasks.points import similarity

    texts = _module_texts(requirement, modules)
    todo: list[tuple[str, list[dict]]] = []
    for e in modules:
        name = str(e.get("module", ""))
        if only is not None and name not in only:
            continue
        pts = [p for p in e.get("points", []) if isinstance(p, dict) and p.get("tp_id") and p.get("status") != "rejected"]
        if len(pts) >= COMBINE_MIN_POINTS:
            todo.append((name, pts))
    if not todo:
        return {"additions": [], "skipped": [], "modules": 0, "failed": 0, "model_name": None}
    sem = asyncio.Semaphore(COMBINE_CONCURRENCY)

    async def _one(name: str, pts: list[dict]) -> tuple[list[dict], list[dict], str | None]:
        view = [{"tp_id": p["tp_id"], "point": p.get("point", ""), "dimension": p.get("dimension", ""),
                 **({"ref": p["ref"]} if p.get("ref") else {}), **({"basis": p["basis"]} if p.get("basis") else {})} for p in pts]
        user = (f"模块「{name}」所在章节的需求原文：\n{wrap_data('需求原文', texts.get(name) or requirement)}\n\n"
                f"模块「{name}」的全部测试点（共 {len(view)} 条；组合只能在这些测试点之间做，from 只能写这里出现的编号）：\n"
                f"{wrap_data('测试点', _dump(view))}\n\n请按五种方法找这个模块内单条规则叠加后的组合场景。") + profile_block()
        async with sem:
            data, result = await _chat_json(llm, [{"role": "system", "content": prompt_text("point_combine")},
                                                  {"role": "user", "content": user}], model, reasoning_effort="high")
        raw = None
        if isinstance(data, dict):
            for key in _COMBINE_LIST_KEYS:
                if isinstance(data.get(key), list):
                    raw = data[key]
                    break
        elif isinstance(data, list):
            raw = data
        ids = {p["tp_id"] for p in pts}
        existing = [str(p.get("point", "")) for p in pts]
        out: list[dict] = []
        skipped: list[dict] = []
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("point") or item.get("text") or item.get("description") or "").strip()
            if not text:
                continue
            frm = next((item[k] for k in _COMBINE_FROM_KEYS if item.get(k)), [])
            if isinstance(frm, str):
                frm = re.split(r"[,，、;；\s]+", frm)
            frm = list(dict.fromkeys(str(x).strip() for x in frm if str(x).strip() in ids)) if isinstance(frm, list) else []
            basis = str(item.get("basis") or item.get("reason") or item.get("依据") or "").strip()
            if len(frm) < 2:
                skipped.append({"module": name, "point": text, "reason": "来源编号不足两条或不在本模块（单点场景不是组合）"})
                continue
            if not basis:
                skipped.append({"module": name, "point": text, "reason": "没有写为什么要测（basis）"})
                continue
            if any(similarity(text, old) >= COMBINE_SIMILAR for old in existing):
                skipped.append({"module": name, "point": text, "reason": "与已有测试点高度相似，已跳过"})
                continue
            if len(out) >= COMBINE_MAX_PER_MODULE:
                skipped.append({"module": name, "point": text, "reason": f"超出每模块组合上限（{COMBINE_MAX_PER_MODULE} 条）"})
                continue
            dim = item.get("dimension")
            out.append({"module": name, "point": text,
                        "dimension": str(dim).strip() if isinstance(dim, (str, int, float)) else "",
                        "ref": str(item.get("ref") or "").strip(), "basis": basis, "combo_from": frm})
            existing.append(text)
        return out, skipped, result.model_name

    outcomes = await asyncio.gather(*[_one(n, p) for n, p in todo], return_exceptions=True)
    additions: list[dict] = []
    skipped: list[dict] = []
    failed = 0
    model_name = None
    for (name, _), o in zip(todo, outcomes):
        if isinstance(o, BaseException):
            failed += 1
            logger.warning("组合延伸模块「{}」失败（只丢该模块）：{}", name, o)
        else:
            additions.extend(o[0])
            skipped.extend(o[1])
            model_name = model_name or o[2]
    if failed == len(todo):
        raise next(o for o in outcomes if isinstance(o, BaseException))
    logger.info("模块级组合延伸：{} 个模块 → 组合 {} 条，跳过 {} 条，故障 {} 个模块", len(todo), len(additions), len(skipped), failed)
    return {"additions": additions, "skipped": skipped, "modules": len(todo), "failed": failed, "model_name": model_name}


# ---- 待确认事项推荐答案（Prompt question_suggest）：原文 › 历史结论 › 行业惯例，人确认才算 ----


QUESTION_SUGGEST_BATCH = 40
QUESTION_BASES = ("原文", "历史结论", "行业惯例")
QUESTION_CONFIDENCE = ("高", "中", "低")
QUESTION_HISTORY_SIM = 0.3   # 历史结论池里问法相似度到这么多的才送给模型参考
QUESTION_HISTORY_PER = 3     # 每条事项最多带这么多条历史结论


async def run_question_suggest(llm: LLMClient, requirement: str, questions: list[dict], history: list[dict],
                               model: str | None) -> dict:
    """给未确认的待确认事项拟推荐答案：按 QUESTION_SUGGEST_BATCH 条一批并行，每批带需求原文、本批事项（question / reason / module /
    level / assumption）与同项目其他需求已确认的相似问题结论（问法相似 ≥ QUESTION_HISTORY_SIM，每条最多 QUESTION_HISTORY_PER 条）。
    程序兜底：index 只认批内编号、一条事项一个答案、answer 为空的丢；basis 不在枚举内按「行业惯例」；basis=原文 的 evidence 用
    `quote_found` 核验，对不上的降为「行业惯例」并标 `unverified`；confidence 不在枚举内按「中」；某批故障只丢该批。
    返回 {"suggestions": {q_id: {answer, basis, evidence, risk, confidence, unverified?}}, "asked", "batches", "failed"}。"""
    import asyncio

    from app.agents.analysis import normalize, quote_found
    from app.requirements import question_similarity

    qs = [q for q in questions if str(q.get("question") or "").strip() and q.get("q_id")]
    if not qs:
        return {"suggestions": {}, "asked": 0, "batches": 0, "failed": 0}
    src = normalize(requirement)
    batches = [qs[i:i + QUESTION_SUGGEST_BATCH] for i in range(0, len(qs), QUESTION_SUGGEST_BATCH)]

    async def _one(batch: list[dict]) -> dict[str, dict]:
        view = []
        for i, q in enumerate(batch, 1):
            row = {"index": i, "question": q["question"], "level": "必须确认" if (q.get("level") or "must") == "must" else "建议确认"}
            for key, label in (("reason", "reason"), ("module", "module"), ("assumption", "assumption")):
                if str(q.get(key) or "").strip():
                    row[label] = str(q[key]).strip()
            view.append(row)
        hist: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for q in batch:
            scored = sorted(((question_similarity(q["question"], str(h.get("question") or "")), h) for h in history),
                            key=lambda x: -x[0])
            for score, h in scored[:QUESTION_HISTORY_PER]:
                key = (str(h.get("question") or ""), str(h.get("answer") or ""))
                if score >= QUESTION_HISTORY_SIM and key not in seen:
                    seen.add(key)
                    hist.append({"question": key[0], "answer": key[1], "requirement": str(h.get("_title") or "")})
        user = (f"需求原文：\n{wrap_data('需求原文', requirement)}\n\n"
                f"待确认事项（共 {len(view)} 条，每条都要给答案，index 用这里的编号）：\n{wrap_data('待确认事项', _dump(view))}\n\n")
        if hist:
            user += (f"同项目其他需求已确认的相似问题结论（只作「历史结论」来源；与本需求原文冲突时以原文为准）：\n"
                     f"{wrap_data('历史结论', _dump(hist))}\n\n")
        user += profile_block()
        data, _ = await _chat_json(llm, [{"role": "system", "content": prompt_text("question_suggest")},
                                         {"role": "user", "content": user}], model)
        out: dict[str, dict] = {}
        for item in (data.get("suggestions") or []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("index"))
            except (TypeError, ValueError):
                continue
            if not 1 <= idx <= len(batch):
                continue
            qid = batch[idx - 1]["q_id"]
            answer = str(item.get("answer") or "").strip()
            if qid in out or not answer:
                continue
            basis = str(item.get("basis") or "").strip()
            evidence = str(item.get("evidence") or "").strip()
            conf = str(item.get("confidence") or "").strip()
            s = {"answer": answer, "basis": basis if basis in QUESTION_BASES else "行业惯例", "evidence": evidence,
                 "risk": str(item.get("risk") or "").strip(), "confidence": conf if conf in QUESTION_CONFIDENCE else "中"}
            if s["basis"] == "原文" and not (evidence and quote_found(evidence, src)):
                s["basis"], s["unverified"] = "行业惯例", True   # 说是原文却引不出来：按惯例看待、标出来
            out[qid] = s
        return out

    outcomes = await asyncio.gather(*[_one(b) for b in batches], return_exceptions=True)
    suggestions: dict[str, dict] = {}
    failed = 0
    for i, o in enumerate(outcomes, 1):
        if isinstance(o, BaseException):
            failed += 1
            logger.warning("待确认事项推荐答案第 {} 批失败（只丢该批）：{}", i, o)
        else:
            suggestions.update(o)
    if failed == len(batches):
        raise next(o for o in outcomes if isinstance(o, BaseException))
    logger.info("待确认事项推荐答案：{} 条 / {} 批 → {} 条有推荐（引文未核验 {}）", len(qs), len(batches), len(suggestions),
                sum(1 for x in suggestions.values() if x.get("unverified")))
    return {"suggestions": suggestions, "asked": len(qs), "batches": len(batches), "failed": failed}


# ---- 执行失败归因与缺陷单（Prompt exec_triage）：人只填一句说明，分类与缺陷单由模型起草 ----


TRIAGE_CATEGORIES = ("用例问题", "系统缺陷", "环境问题", "数据问题", "需求变更")
TRIAGE_SUBS = ("步骤有误", "预期有误", "前置缺失")
TRIAGE_SEVERITIES = ("致命", "严重", "一般", "轻微")
# 五类归因 → 执行记录的失败分类（FAIL_REASONS）：前两类进学习语料，所以「用例问题」按 sub 细分；需求变更没有对应项记「其他」
_TRIAGE_REASON = {("用例问题", "步骤有误"): "用例步骤有误", ("用例问题", "预期有误"): "用例预期有误", ("用例问题", "前置缺失"): "用例步骤有误",
                  ("用例问题", ""): "用例步骤有误", "系统缺陷": "系统缺陷", "环境问题": "环境问题", "数据问题": "测试数据问题", "需求变更": "其他"}


def triage_reason(category: str, sub: str = "") -> str:
    return _TRIAGE_REASON.get((category, sub)) or _TRIAGE_REASON.get(category) or "其他"


async def run_exec_triage(llm: LLMClient, case: dict, note: str, requirement: str, screenshots: list[str], model: str | None,
                          points: list[dict] | None = None) -> dict:
    """执行失败归因：用例（标题 / 前置 / 步骤与预期 / 来源测试点）+ 执行说明 + 失败截图的理解结果 + 该用例对应的需求原文 → 一次调用。
    程序兜底：category 不在五类内按「系统缺陷」且 confidence「低」（不丢，交人看）；sub 只在用例问题下认三类；failed_step 收到 0～步数；
    confidence 枚举外按「中」；bug 只在系统缺陷时保留，severity 枚举外按「一般」，steps 只认字符串；`suggested_reason` 为映射到
    FAIL_REASONS 的建议分类，人采纳才写进执行记录。返回 {"triage": {...}, "bug": {...} | None, "model_name"}。"""
    steps = [s for s in (case.get("steps") or []) if isinstance(s, dict)]
    view = {"title": case.get("title", ""), "module": case.get("module", ""), "priority": case.get("priority", ""),
            "precondition": case.get("precondition", ""),
            "steps": [{"no": i, "action": s.get("action", ""), "expected": s.get("expected", "")} for i, s in enumerate(steps, 1)],
            **({"remark": case["remark"]} if case.get("remark") else {})}
    user = f"用例：\n{wrap_data('用例', _dump(view))}\n\n执行人的失败说明：\n{wrap_data('失败说明', note or '（没填）')}\n\n"
    if points:
        user += f"用例的来源测试点：\n{wrap_data('测试点', _dump(points))}\n\n"
    for i, shot in enumerate(screenshots or [], 1):
        user += f"失败截图 {i} 的理解结果：\n{wrap_data(f'截图{i}', shot)}\n\n"
    user += (f"该用例对应的需求原文：\n{wrap_data('需求原文', requirement or '（没有找到对应原文）')}\n\n"
             "请归因并（仅系统缺陷时）起草缺陷单。") + profile_block()
    data, result = await _chat_json(llm, [{"role": "system", "content": prompt_text("exec_triage")},
                                          {"role": "user", "content": user}], model)
    raw = data.get("triage") if isinstance(data, dict) else None
    raw = raw if isinstance(raw, dict) else (data if isinstance(data, dict) and data.get("category") else {})
    category = str(raw.get("category") or "").strip()
    confidence = str(raw.get("confidence") or "").strip()
    if category not in TRIAGE_CATEGORIES:
        category, confidence = "系统缺陷", "低"
    sub = str(raw.get("sub") or "").strip()
    sub = sub if category == "用例问题" and sub in TRIAGE_SUBS else ""
    try:
        step = int(raw.get("failed_step") or 0)
    except (TypeError, ValueError):
        step = 0
    triage = {"category": category, "sub": sub, "failed_step": max(0, min(step, len(steps))),
              "confidence": confidence if confidence in ("高", "中", "低") else "中",
              "reason": str(raw.get("reason") or "").strip(), "suggested_reason": triage_reason(category, sub)}
    bug = None
    rb = data.get("bug") if isinstance(data, dict) else None
    if category == "系统缺陷" and isinstance(rb, dict) and str(rb.get("title") or "").strip():
        sev = str(rb.get("severity") or "").strip()
        bug = {"title": str(rb.get("title") or "").strip()[:60], "severity": sev if sev in TRIAGE_SEVERITIES else "一般",
               "precondition": str(rb.get("precondition") or "").strip(),
               "steps": [str(s).strip() for s in (rb.get("steps") or []) if isinstance(s, (str, int, float)) and str(s).strip()],
               "expected": str(rb.get("expected") or "").strip(), "actual": str(rb.get("actual") or "").strip(),
               "scope": str(rb.get("scope") or "").strip()}
    logger.info("执行失败归因：{}（{}，置信度{}）{}", category, sub or "-", triage["confidence"], "，起草缺陷单" if bug else "")
    return {"triage": triage, "bug": bug, "model_name": result.model_name}


# ---- 重复复核（需求十一~十三）----


# 一次裁判调用最多看多少对：一次塞上千对模型只会判前面一百多对，其余全成「待复核」压给人工
DUP_JUDGE_BATCH = 40
# 文字完全一样、裁判却判不重复的对（不同页面 / 对象各验一次）在列表里的判定文字
SAME_TEXT_VERDICT = "同文不同对象"


def _pair_view(p: dict) -> dict:
    """送裁判的一对：编号、所属模块、测试点文本；模块名就是页面 / 功能，裁判靠它判断两条是不是同一个测试对象。"""
    return {"a": p["a"], "a_module": p.get("a_module", ""), "a_point": p["a_point"],
            "b": p["b"], "b_module": p.get("b_module", ""), "b_point": p["b_point"]}


async def run_dup_judge(
    llm: LLMClient, requirement: str, pairs: list[dict], model: str | None
) -> list[dict]:
    """对文字初筛的疑似重复对做语义复核：按 DUP_JUDGE_BATCH 分批并行，每批带两边模块名；
    某批 LLM 故障时该批原样返回初筛结果标「待复核」（降级不阻塞）。复核为不重复的对不再打扰人工——
    但两条文字完全一样却判不重复的（日榜 / 月榜各一条「商户名称取会员所属商户名称」）留在列表里标「同文不同对象」，
    人看到同一句话出现两遍时能知道这是 AI 有意保留的两个测试对象，确认后点忽略、不认可就删一条。"""
    if not pairs:
        return []
    import asyncio
    from app.tasks.points import same_point_text

    async def _batch(batch: list[dict]) -> dict[tuple[str, str], dict]:
        data, _ = await _chat_json(
            llm,
            [
                {"role": "system", "content": prompt_text("dup_judge")},
                {
                    "role": "user",
                    "content": f"需求内容：\n{wrap_data('需求原文', requirement[:2000])}\n\n疑似重复测试点对：\n"
                               f"{wrap_data('测试点对', _dump([_pair_view(p) for p in batch]))}",
                },
            ],
            model,
        )
        return {(str(d.get("a")), str(d.get("b"))): d for d in data.get("duplicates", []) if isinstance(d, dict)}

    batches = [pairs[i:i + DUP_JUDGE_BATCH] for i in range(0, len(pairs), DUP_JUDGE_BATCH)]
    outcomes = await asyncio.gather(*(_batch(b) for b in batches), return_exceptions=True)
    judged: dict[tuple[str, str], dict] = {}
    failed: set[str] = set()
    for batch, out in zip(batches, outcomes):
        if isinstance(out, Exception):
            logger.warning("重复复核一批（{} 对）LLM 故障，该批降级为文字初筛结果：{}", len(batch), out)
            failed.update(p["a"] + "|" + p["b"] for p in batch)
            continue
        judged.update(out)
    if len(batches) > 1:
        logger.info("重复复核 {} 对分 {} 批，{} 批成功", len(pairs), len(batches), len(batches) - sum(isinstance(o, Exception) for o in outcomes))
    merged = []
    for p in pairs:
        if p["a"] + "|" + p["b"] in failed:
            merged.append(dict(p, verdict="待复核", suggestion="LLM 复核失败，请人工判断"))
            continue
        j = judged.get((p["a"], p["b"])) or judged.get((p["b"], p["a"])) or {}
        verdict = str(j.get("verdict", "待复核"))
        suggestion = str(j.get("suggestion", "建议合并或保留其中一条"))
        if verdict == "不重复" and same_point_text(p.get("a_point", ""), p.get("b_point", "")):
            verdict = SAME_TEXT_VERDICT
            suggestion = "同一条规则在两个页面 / 对象下各验一次；确认保留两条点「忽略」，认为只需一条就删掉其中一条"
        merged.append(dict(p, verdict=verdict, reason=str(j.get("reason", p.get("reason", ""))), suggestion=suggestion))
    return [p for p in merged if p["verdict"] != "不重复"]


# ---- 人工 vs AI 测试点对比 ----


# ---- 用例语义查重裁判 ----

CASE_DUP_VERDICTS = ("重复", "可合并", "不重复")


def _case_side(c: dict | None) -> dict:
    if not c:
        return {}
    return {"module": c.get("module", ""), "title": c.get("title", ""), "precondition": c.get("precondition", ""),
            "steps": [{"action": st.get("action", ""), "expected": st.get("expected", "")} for st in (c.get("steps") or [])],
            "point_ids": c.get("point_ids") or []}


async def run_case_dup_judge(llm: LLMClient, requirement: str, pairs: list[dict], cases: list[dict], model: str | None) -> list[dict]:
    """用例对的语义复核：`case_duplicate_candidates` 的文字候选（标题 + 前置 + 步骤 + 预期 Jaccard ≥ 0.6）按 DUP_JUDGE_BATCH 对一批并行，
    每对带两条用例的模块 / 标题 / 前置 / 步骤 / 来源测试点，裁判三选一；判「不重复」的不进列表，没判到的标「待复核」；
    某批故障该批全标待复核。AI 不删，人工处置。"""
    if not pairs:
        return []
    import asyncio
    by_id = {str(c.get("case_id")): c for c in cases}

    async def _batch(batch: list[dict]) -> dict[tuple[str, str], dict]:
        view = [{"a": p["a"], "a_case": _case_side(by_id.get(p["a"])), "b": p["b"], "b_case": _case_side(by_id.get(p["b"]))} for p in batch]
        data, _ = await _chat_json(
            llm,
            [{"role": "system", "content": prompt_text("case_dup_judge")},
             {"role": "user", "content": f"需求内容：\n{wrap_data('需求原文', requirement[:2000])}\n\n疑似重复用例对：\n{wrap_data('用例对', _dump(view))}"}],
            model,
        )
        out: dict[tuple[str, str], dict] = {}
        for d in (data.get("duplicates") or []) if isinstance(data, dict) else []:
            if not isinstance(d, dict):
                continue
            verdict = str(d.get("verdict") or "").strip()
            if verdict not in CASE_DUP_VERDICTS:
                continue
            keep = str(d.get("keep") or "").strip().lower()
            out[(str(d.get("a")), str(d.get("b")))] = {
                "verdict": verdict, "reason": str(d.get("reason") or ""),
                "keep": keep if keep in ("a", "b") else "", "merged_title": str(d.get("merged_title") or "")}
        return out

    batches = [pairs[i:i + DUP_JUDGE_BATCH] for i in range(0, len(pairs), DUP_JUDGE_BATCH)]
    outcomes = await asyncio.gather(*[_batch(b) for b in batches], return_exceptions=True)
    judged: list[dict] = []
    for batch, res in zip(batches, outcomes):
        if isinstance(res, BaseException):
            logger.warning("用例查重裁判一批失败（{} 对标待复核）：{}", len(batch), res)
            judged.extend({**p, "verdict": "待复核", "reason": "AI 复核失败，请人工判断"} for p in batch)
            continue
        for p in batch:
            r = res.get((p["a"], p["b"])) or res.get((p["b"], p["a"]))
            if r is None:
                judged.append({**p, "verdict": "待复核", "reason": "AI 未给出结论"})
            elif r["verdict"] != "不重复":
                judged.append({**p, **r})
    logger.info("用例查重：候选 {} 对 → 重复 {} / 可合并 {} / 待复核 {}", len(pairs),
                sum(1 for j in judged if j["verdict"] == "重复"), sum(1 for j in judged if j["verdict"] == "可合并"),
                sum(1 for j in judged if j["verdict"] == "待复核"))
    return judged


async def run_point_compare(llm: LLMClient, requirement: str, modules: list[dict], model: str | None) -> dict:
    """人工测试点与 AI 测试点逐条语义匹配；LLM 故障时降级为文字相似配对（标注待复核）。"""
    from app.tasks.points import AI_SOURCES, cross_source_candidates, iter_points

    flat = [dict(p, module=e["module"]) for e, p in iter_points(modules)]
    manual = [p for p in flat if p.get("source") == "manual"]
    ai = [p for p in flat if p.get("source", "ai") in AI_SOURCES]
    by_id = {p["tp_id"]: p for p in flat}
    view = lambda ps: [{"tp_id": p["tp_id"], "module": p["module"], "point": p["point"]} for p in ps]  # noqa: E731
    degraded = False
    model_name = None
    if manual and ai:
        try:
            data, result = await _chat_json(
                llm,
                [
                    {"role": "system", "content": prompt_text("point_compare")},
                    {"role": "user", "content": f"需求内容：\n{wrap_data('需求原文', requirement[:4000])}\n\n"
                                                f"人工编写的测试点：\n{wrap_data('人工测试点', _dump(view(manual)))}\n\n"
                                                f"AI 生成的测试点：\n{wrap_data('AI 测试点', _dump(view(ai)))}"},
                ],
                model,
            )
            model_name = getattr(result, "model_name", None)
        except Exception as e:
            logger.warning("人工/AI 测试点对比 LLM 故障，降级为文字相似配对：{}", e)
            degraded = True
            data = {"matches": [{"manual": p["manual"], "ai": p["ai"], "verdict": "待复核",
                                 "reason": f"文字相似度 {p['similarity']}，LLM 复核失败请人工判断"}
                                for p in cross_source_candidates(modules)]}
    else:
        data = {"matches": []}

    matched: list[dict] = []
    used_manual: set[str] = set()
    used_ai: set[str] = set()
    manual_ids = {p["tp_id"] for p in manual}
    ai_ids = {p["tp_id"] for p in ai}
    for m in data.get("matches", []) or []:
        if not isinstance(m, dict):
            continue
        mid, aid = str(m.get("manual", "")), str(m.get("ai", ""))
        if mid not in manual_ids or aid not in ai_ids or mid in used_manual or aid in used_ai:
            continue
        used_manual.add(mid)
        used_ai.add(aid)
        verdict = str(m.get("verdict", "一致"))
        matched.append({"manual": mid, "manual_point": by_id[mid]["point"], "ai": aid, "ai_point": by_id[aid]["point"],
                        "module": by_id[mid]["module"], "verdict": verdict if verdict in ("一致", "部分覆盖", "待复核") else "部分覆盖",
                        "reason": str(m.get("reason", ""))})
    reasons_m = {str(x.get("tp_id")): x for x in data.get("manual_only", []) or [] if isinstance(x, dict)}
    reasons_a = {str(x.get("tp_id")): x for x in data.get("ai_only", []) or [] if isinstance(x, dict)}
    manual_only = [{"tp_id": p["tp_id"], "point": p["point"], "module": p["module"],
                    "reason": str(reasons_m.get(p["tp_id"], {}).get("reason", ""))}
                   for p in manual if p["tp_id"] not in used_manual]
    ai_only = [{"tp_id": p["tp_id"], "point": p["point"], "module": p["module"], "source": p.get("source", "ai"),
                "reason": str(reasons_a.get(p["tp_id"], {}).get("reason", "")),
                "valuable": reasons_a.get(p["tp_id"], {}).get("valuable")}
               for p in ai if p["tp_id"] not in used_ai]
    exact = sum(1 for m in matched if m["verdict"] == "一致")
    stats = {
        "manual_total": len(manual), "ai_total": len(ai), "matched": len(matched), "exact": exact,
        "partial": len(matched) - exact, "manual_only": len(manual_only), "ai_only": len(ai_only),
        # AI 对人工测试点的覆盖率：人工写的点里有多少被 AI 找到了
        "ai_recall": round(len(matched) / len(manual), 3) if manual else None,
        # AI 结果里与人工一致的占比：人工视角下 AI 的「命中率」
        "ai_precision": round(len(matched) / len(ai), 3) if ai else None,
    }
    return {"matched": matched, "manual_only": manual_only, "ai_only": ai_only, "stats": stats,
            "degraded": degraded, "model_name": model_name}


# ---- 测试点驳回定点修改（需求三十~三十三）----


async def run_point_fix(
    llm: LLMClient, requirement: str, modules: list[dict], rejected: list[dict], model: str | None
) -> dict:
    """AI 只获取被驳回测试点 + 结构化驳回信息（需求三十一 / 完整需求 7.1）。

    返回修改**提案**（完整需求 7.3 确认流）：不直接应用，逐项接受/拒绝后经
    apply_point_proposals 落地。
    """
    payload = [
        {"tp_id": p["tp_id"], "module": p.get("module", ""), "point": p["point"],
         "dimension": p.get("dimension", ""),
         "驳回类型": p.get("reject_types") or [], "驳回原因": p.get("comment", ""),
         "修改要求": p.get("fix_request", ""), "修改范围": p.get("fix_scope", "") or "当前项"}
        for p in rejected
    ]
    data, result = await _chat_json(
        llm,
        [
            {"role": "system", "content": prompt_text("point_fix")},
            {
                "role": "user",
                "content": f"关联需求：\n{wrap_data('需求原文', requirement)}\n\n被驳回的测试点与审核意见：\n{wrap_data('测试点与审核意见', _dump(payload))}",
            },
        ],
        model,
    )
    proposals = build_point_proposals(modules, data, allowed={p["tp_id"] for p in rejected})
    return {"proposals": proposals, "model_name": result.model_name}


def build_point_proposals(modules: list[dict], data: dict, allowed: set[str]) -> list[dict]:
    """AI 输出 → 修改提案列表（越权项在此过滤；不改动 modules）。"""
    proposals: list[dict] = []
    seq = 0
    for fix in data.get("fixes", []):
        tp_id = str(fix.get("tp_id", ""))
        if tp_id not in allowed:  # 模型越权改动非驳回项：直接丢弃
            logger.warning("测试点定点修改越权改动 {}（非驳回项），已忽略", tp_id)
            continue
        found = find_point(modules, tp_id)
        if found is None:
            continue
        _, point = found
        action = str(fix.get("action", "modify"))
        entry = {
            "proposal_id": f"P{(seq := seq + 1)}", "tp_id": tp_id, "action": action,
            "comment_type": str(fix.get("comment_type", "其他")),
            "comment": point.get("comment", ""), "note": str(fix.get("note", "")),
            "before": point.get("point", ""), "raw": fix,
        }
        if action == "split":
            split_into = [s for s in fix.get("split_into", [])
                          if isinstance(s, dict) and str(s.get("point", "")).strip()]
            if not split_into:
                continue
            entry["after"] = [str(s["point"]).strip() for s in split_into]
        elif action == "delete":
            entry["after"] = None
        else:
            text = str(fix.get("point", "")).strip()
            if not text:
                continue
            entry["after"] = text
        proposals.append(entry)
    for a in data.get("additions", []):
        if not (isinstance(a, dict) and str(a.get("point", "")).strip()):
            continue
        proposals.append({
            "proposal_id": f"P{(seq := seq + 1)}", "tp_id": None, "action": "add",
            "comment_type": "场景遗漏", "note": str(a.get("reason", "")),
            "before": None, "after": str(a["point"]).strip(),
            "module": str(a.get("module", "")).strip(), "raw": a,
        })
    return proposals


def apply_point_proposals(modules: list[dict], accepted: list[dict], seq_floor: int = 0) -> dict:
    """确认流落地：只应用被接受的提案（复用 apply_point_fixes 的确定性合并）。

    seq_floor 传任务持久化的最大编号：新增测试点不复用已删除的编号（版本历史与回收站按 tp_id 关联）。
    """
    data = {
        "fixes": [p["raw"] for p in accepted if p.get("action") != "add"],
        "additions": [p["raw"] for p in accepted if p.get("action") == "add"],
    }
    return apply_point_fixes(modules, data, allowed={p["tp_id"] for p in accepted if p.get("tp_id")},
                             seq_floor=seq_floor)


def apply_point_fixes(modules: list[dict], data: dict, allowed: set[str], seq_floor: int = 0) -> dict:
    """确定性合并测试点修改：只允许改动被驳回项（铁律兜底），产出 Diff（需求三十四）。

    修改后的测试点回到 pending 待再审核（需求六十一：局部修改 → 再审核），保留驳回计数。
    """
    diff: list[dict] = []
    for fix in data.get("fixes", []):
        tp_id = str(fix.get("tp_id", ""))
        if tp_id not in allowed:  # 模型越权改动非驳回项：直接丢弃
            logger.warning("测试点定点修改越权改动 {}（非驳回项），已忽略", tp_id)
            continue
        found = find_point(modules, tp_id)
        if found is None:
            continue
        entry, point = found
        action = str(fix.get("action", "modify"))
        comment_type = str(fix.get("comment_type", "其他"))
        record = {"tp_id": tp_id, "action": action, "comment_type": comment_type,
                  "comment": point.get("comment", ""), "note": str(fix.get("note", "")),
                  "before": point.get("point", "")}
        if action == "split":
            split_into = [
                s for s in fix.get("split_into", [])
                if isinstance(s, dict) and str(s.get("point", "")).strip()
            ]
            if not split_into:
                continue
            idx = entry["points"].index(point)
            existing = {str(p.get("tp_id", "")) for _, p in iter_points(modules)}
            entry["points"].remove(point)
            new_points = []
            n = 0
            for i, s in enumerate(split_into):
                n += 1
                while f"{tp_id}-{n:02d}" in existing:  # 同一点再次拆分 / 回收站恢复后再拆：编号不得撞车
                    n += 1
                existing.add(f"{tp_id}-{n:02d}")
                new_points.append({
                    "tp_id": f"{tp_id}-{n:02d}",
                    "point": str(s["point"]).strip(),
                    "dimension": str(s.get("dimension", point.get("dimension", ""))).strip(),
                    "status": "pending", "locked": False, "comment": "",
                    "reject_count": point.get("reject_count", 0), "source": "ai", "version": 1,
                    "warnings": coarse_warnings(str(s["point"])),
                })
            entry["points"][idx:idx] = new_points
            record["after"] = [p["point"] for p in new_points]
        elif action == "delete":
            entry["points"].remove(point)
            record["after"] = None
        else:  # modify
            text = str(fix.get("point", "")).strip()
            if not text:
                continue
            point["point"] = text
            point["version"] = int(point.get("version", 1)) + 1
            if str(fix.get("dimension", "")).strip():
                point["dimension"] = str(fix["dimension"]).strip()
            point["status"], point["locked"] = "pending", False
            clear_reject_fields(point)
            point["warnings"] = coarse_warnings(text)
            record["after"] = text
        diff.append(record)
    added = add_points(
        modules,
        [a for a in data.get("additions", []) if isinstance(a, dict)],
        source="ai_fix", seq_floor=seq_floor,
    )
    return {"diff": diff, "added": added}


# ---- 用例驳回定点修改（需求三十一/三十三/五十四）----


async def run_case_fix(
    llm: LLMClient,
    requirement: str,
    cases: list[dict],
    reviews: dict[str, dict],
    template: CustomTemplate | None,
    model: str | None,
) -> dict:
    """AI 只获取被驳回用例 + 结构化驳回信息；锁定用例确定性保护。

    返回修改**提案**（完整需求 9.4 确认流）：不直接应用，逐项接受/拒绝后经
    apply_case_proposals 落地。
    """
    template = template or builtin_default_template()
    rejected = [
        c for c in cases
        if reviews.get(str(c.get("uid") or ""), {}).get("status") == "rejected"
    ]
    if not rejected:
        return {"proposals": [], "invalid": [], "model_name": None}
    payload = [
        dict(_strip_case(c), **_fix_directives(reviews[str(c.get("uid"))]))
        for c in rejected
    ]
    system = prompt_text("case_fix").format(template_spec=template.prompt_spec())
    data, result = await _chat_json(
        llm,
        [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": f"关联需求：\n{wrap_data('需求原文', requirement)}\n\n被驳回的用例与审核意见：\n{wrap_data('用例与审核意见', _dump(payload))}",
            },
        ],
        model,
    )
    allowed = {str(c.get("case_id")) for c in rejected}
    outcome = build_case_proposals(cases, data, allowed, reviews)
    outcome["model_name"] = result.model_name
    return outcome


def build_case_proposals(
    cases: list[dict], data: dict, allowed: set[str], reviews: dict[str, dict]
) -> dict:
    """AI 输出 → 用例修改提案列表（越权过滤 + 字段/步骤定位兜底预先应用；不改动 cases）。"""
    by_id = {str(c.get("case_id")): c for c in cases}
    meta = {str(f.get("case_id", "")): f for f in data.get("fixes", []) if isinstance(f, dict)}
    proposals: list[dict] = []
    invalid: list[dict] = []
    seq = 0
    for raw in data.get("cases", []):
        if not isinstance(raw, dict):
            continue
        try:
            TestCase.model_validate({**raw, "uid": ""}, context={"require_expected": True})
        except ValidationError as e:
            invalid.append({"case_id": str(raw.get("case_id", "")), "problem": str(e)})
            continue
        cid = str(raw.get("case_id"))
        origin = by_id.get(cid)
        m = meta.get(cid, {})
        if origin is None:  # 新增用例（拆分/场景遗漏）
            seq += 1
            proposals.append({
                "proposal_id": f"P{seq}", "case_id": cid, "uid": None, "action": "add",
                "comment_type": str(m.get("comment_type", "其他")), "note": str(m.get("note", "")),
                "before": None, "after": _strip_case(raw), "changes": [], "raw": raw,
            })
            continue
        if cid not in allowed:  # 越权修改锁定/未驳回用例：丢弃改动
            logger.warning("用例定点修改越权改动 {}（非驳回项），已忽略", cid)
            continue
        uid = str(origin.get("uid") or "")
        review = reviews.get(uid, {})
        restricted = _restrict_case_fix(
            origin, dict(raw), review.get("fields") or [], review.get("steps") or [])
        changes = _case_field_changes(origin, restricted)
        if not changes:
            # 模型的改动全部落在定位范围之外（已还原）或与原值相同：明确告知，而不是静默「无提案」
            scope = []
            if review.get("fields"):
                scope.append("字段 " + "/".join(review["fields"]))
            if review.get("steps"):
                scope.append("步骤 " + ",".join(str(n) for n in review["steps"]))
            invalid.append({"case_id": cid, "problem": ("AI 的改动不在驳回时指定的范围内（" + "、".join(scope) + "），已按原值保留；可放宽定位后重试或人工修改")
                            if scope else "AI 返回的内容与原用例相同，没有实质修改；可补充修改要求后重试或人工修改"})
            continue
        seq += 1
        proposals.append({
            "proposal_id": f"P{seq}", "case_id": cid, "uid": uid, "action": "modify",
            "comment_type": str(m.get("comment_type", "其他")), "note": str(m.get("note", "")),
            "before": _strip_case(origin), "after": _strip_case(restricted),
            "changes": changes, "raw": restricted,
        })
    for cid in data.get("deleted", []):
        cid = str(cid)
        if cid not in allowed or cid not in by_id:
            continue
        m = meta.get(cid, {})
        seq += 1
        proposals.append({
            "proposal_id": f"P{seq}", "case_id": cid, "uid": str(by_id[cid].get("uid") or ""),
            "action": "delete", "comment_type": str(m.get("comment_type", "其他")),
            "note": str(m.get("note", "")), "before": _strip_case(by_id[cid]),
            "after": None, "changes": [], "raw": None,
        })
    return {"proposals": proposals, "invalid": invalid}


def apply_case_proposals(
    cases: list[dict], accepted: list[dict], reviews: dict[str, dict]
) -> dict:
    """确认流落地：只应用被接受的提案（复用 merge_case_fix 的确定性合并与编号顺延）。"""
    data = {
        "cases": [p["raw"] for p in accepted if p["action"] in ("modify", "add")],
        "deleted": [p["case_id"] for p in accepted if p["action"] == "delete"],
    }
    allowed = {p["case_id"] for p in accepted if p["action"] in ("modify", "delete")}
    return merge_case_fix(cases, data, allowed, reviews)


def _strip_case(c: dict) -> dict:
    return {k: v for k, v in c.items() if k != "uid"}


def _fix_directives(review: dict) -> dict:
    """结构化驳回信息 → AI 输入（完整需求 9.3：类型/原因/修改要求/指定字段/指定步骤/范围）。"""
    directives = {
        "驳回类型": review.get("reject_types") or [],
        "驳回原因": review.get("comment", ""),
        "修改要求": review.get("fix_request", ""),
        "修改范围": review.get("fix_scope", "") or "当前项",
    }
    if review.get("fields"):
        directives["指定字段"] = review["fields"]
    if review.get("steps"):
        directives["指定步骤"] = review["steps"]
    return directives


def _restrict_case_fix(origin: dict, new: dict, fields: list, step_nos: list) -> dict:
    """字段/步骤最小修改原则的确定性兜底（完整需求 9.4）。

    人工做了字段级/步骤级定位时，AI 对定位之外内容的改动一律还原为原值——
    与锁定保护同理，不依赖模型自觉。
    """
    if not fields and not step_nos:
        return new
    restricted = dict(new)
    for f in ("module", "title", "priority", "precondition", "keywords", "remark"):
        if fields and f not in fields:
            restricted[f] = origin.get(f, "")
    o_steps = [dict(s) for s in origin.get("steps") or []]
    n_steps = [dict(s) for s in new.get("steps") or []]
    if fields and "steps" not in fields and "expected" not in fields and not step_nos:
        restricted["steps"] = o_steps  # 定位不含步骤/预期：步骤整体不允许动
        return restricted
    if len(n_steps) != len(o_steps) and (step_nos or (fields and "steps" not in fields)):
        # 做了步骤级定位（或只允许改预期）时模型却增删了步骤：越权，步骤整体还原
        logger.warning("用例定点修改越权增删步骤（{} → {} 步），已还原", len(o_steps), len(n_steps))
        restricted["steps"] = o_steps
        return restricted
    if len(n_steps) == len(o_steps):
        merged = []
        for i, (o, n) in enumerate(zip(o_steps, n_steps), start=1):
            if step_nos and i not in step_nos:
                merged.append(o)
                continue
            # 「操作步骤」定位 = 整个步骤（动作 + 预期）可改；只勾「预期结果」时仅预期可改
            action = n.get("action", "") if (not fields or "steps" in fields) else o.get("action", "")
            expected = n.get("expected", "") if (not fields or "steps" in fields or "expected" in fields) else o.get("expected", "")
            merged.append({"action": action, "expected": expected})
        restricted["steps"] = merged
    return restricted


_CASE_SEQ_RE = re.compile(r"^(.*?)(\d+)\s*$")


def merge_case_fix(
    cases: list[dict], data: dict, allowed: set[str], reviews: dict[str, dict]
) -> dict:
    """确定性合并用例修改：锁定/未驳回用例不动，编号不重排（审核期编号保持稳定）。

    - 只接受 allowed（被驳回 case_id）范围内的修改与删除，越权改动丢弃；
    - 新增用例按所在模块现有编号顺延；修改后的用例回到 pending 待再审核；
    - 返回字段级 Diff（需求三十四：修改前 vs 修改后）。
    """
    changed: dict[str, dict] = {}
    invalid: list[dict] = []
    for raw in data.get("cases", []):
        if not isinstance(raw, dict):
            continue
        try:
            TestCase.model_validate({**raw, "uid": ""}, context={"require_expected": True})
        except ValidationError as e:
            invalid.append({"case_id": str(raw.get("case_id", "")), "problem": str(e)})
            continue
        changed[str(raw.get("case_id"))] = raw
    deleted = {str(x) for x in data.get("deleted", [])} & allowed

    diff: list[dict] = []
    result: list[dict] = []
    for case in cases:
        cid = str(case.get("case_id"))
        uid = str(case.get("uid") or "")
        if cid in deleted:
            diff.append({"case_id": cid, "action": "delete", "before": _strip_case(case)})
            reviews.pop(uid, None)
            continue
        if cid in changed and cid in allowed:
            review = reviews.get(uid, {})
            new = _restrict_case_fix(
                case, dict(changed.pop(cid)),
                review.get("fields") or [], review.get("steps") or [],
            )
            new["uid"] = uid or new_uid()
            new["version"] = int(case.get("version", 1)) + 1  # 乐观锁版本推进（需求 11）
            diff.append({
                "case_id": cid, "action": "modify",
                "changes": _case_field_changes(case, new),
            })
            reviews[new["uid"]] = {"status": "pending", "comment": "",
                                   "reject_count": reviews.get(uid, {}).get("reject_count", 0),
                                   "locked": False}
            result.append(new)
            continue
        if cid in changed:  # 越权修改锁定/未驳回用例：丢弃改动，保留原样
            logger.warning("用例定点修改越权改动 {}（非驳回项），已忽略", cid)
            changed.pop(cid)
        result.append(case)

    # 剩余为新增用例：编号按所在模块顺延，不重排存量
    counters: dict[str, int] = {}
    prefixes: dict[str, str] = {}
    for case in result:
        m = _CASE_SEQ_RE.match(str(case.get("case_id", "")))
        module = str(case.get("module", ""))
        if m:
            counters[module] = max(counters.get(module, 0), int(m.group(2)))
            prefixes.setdefault(module, m.group(1))
    for raw in changed.values():
        new = dict(raw)
        module = str(new.get("module", ""))
        counters[module] = counters.get(module, 0) + 1
        prefix = prefixes.get(module, f"TC-{module}-")
        new["case_id"] = f"{prefix}{counters[module]:03d}"
        new["uid"] = new_uid()
        result.append(new)
        diff.append({"case_id": new["case_id"], "action": "add", "after": _strip_case(new)})

    return {"cases": result, "diff": diff, "invalid": invalid}


def _case_field_changes(before: dict, after: dict) -> list[dict]:
    changes: list[dict] = []
    for field in ("module", "title", "priority", "precondition", "keywords", "remark"):
        b, a = str(before.get(field, "") or ""), str(after.get(field, "") or "")
        if b != a:
            changes.append({"field": field, "before": b, "after": a})
    fmt = lambda c: "\n".join(  # noqa: E731
        f"{s.get('action', '')} => {s.get('expected', '')}" for s in c.get("steps") or []
    )
    if fmt(before) != fmt(after):
        changes.append({"field": "steps", "before": fmt(before), "after": fmt(after)})
    return changes


# ---- 需求变更差异分析（需求四十~四十五）----


async def run_requirement_diff(
    llm: LLMClient,
    old_requirement: str,
    new_requirement: str,
    modules: list[dict],
    cases: list[dict],
    model: str | None,
) -> dict:
    case_index = [
        {"case_id": str(c.get("case_id")), "title": str(c.get("title"))} for c in cases
    ]
    data, result = await _chat_json(
        llm,
        [
            {"role": "system", "content": prompt_text("requirement_diff")},
            {
                "role": "user",
                "content": (
                    f"旧版需求：\n{wrap_data('旧版需求', old_requirement)}\n\n新版需求：\n{wrap_data('新版需求', new_requirement)}\n\n"
                    f"现有测试点：\n{_dump(_points_view(modules))}\n\n"
                    f"现有用例索引：\n{_dump(case_index)}"
                ),
            },
        ],
        model,
    )
    known_points = {p.get("tp_id") for e in modules for p in e.get("points", [])}
    known_cases = {str(c.get("case_id")) for c in cases}
    changes = []
    for ch in data.get("changes", []):
        if not isinstance(ch, dict):
            continue
        changes.append({
            "type": str(ch.get("type", "修改")),
            "description": str(ch.get("description", "")),
            "affected_points": [str(t) for t in (ch.get("affected_points") or []) if isinstance(t, (str, int)) and str(t) in known_points],
            "affected_cases": [str(c) for c in (ch.get("affected_cases") or []) if isinstance(c, (str, int)) and str(c) in known_cases],
            "action_hint": str(ch.get("action_hint", "")),
        })
    return {
        "changes": changes,
        "new_requirements": [str(x) for x in data.get("new_requirements", [])],
        "summary": str(data.get("summary", "")),
        "model_name": result.model_name,
    }


# ---- 学习候选提炼（需求三十六~三十八）----


async def run_learning_analysis(
    llm: LLMClient, samples: list[dict], model: str | None, project: str | None = None,
) -> list[dict]:
    """从人工修改留痕提炼规则候选；样本不足或无模式时返回空列表。

    project：这批留痕所属的项目（调用方按项目分组后各调一次），写进 user 消息让 scope_hint 的「project」有所指；
    None 表示没有项目归属的留痕。
    """
    if not samples:
        return []
    head = (f"以下留痕全部来自项目「{project}」（scope_hint 的 project 指它；只有确实与项目业务无关、换个项目也成立的写法习惯才建议 system / team）。"
            if project else "以下留痕没有项目归属（任务未指定项目）。")
    data, _ = await _chat_json(
        llm,
        [
            {"role": "system", "content": prompt_text("learning")},
            {"role": "user", "content": f"{head}\n人工修改留痕样本（共 {len(samples)} 条）：\n{wrap_data('修改留痕', _dump(samples))}"},
        ],
        model,
    )
    candidates = []
    for c in data.get("candidates", []):
        if not isinstance(c, dict) or not str(c.get("content", "")).strip():
            continue
        candidates.append({
            "content": str(c["content"]).strip(),
            "evidence": str(c.get("evidence", "")),
            "occurrences": int(c.get("occurrences", 0) or 0),
            "confidence": str(c.get("confidence", "中")),
            "scope_hint": str(c.get("scope_hint", "project")),
        })
    return candidates
