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
        coverage = merge_coverage([c for c, _, _ in done])
        additions = [a for _, adds, _ in done for a in adds]
        model_name = done[0][2]
        logger.info("查漏按章节分 {} 批（每批 ≤ {} 字），{} 批完成", len(batches), GAP_BATCH_CHARS, len(done))
    logger.info(
        "独立查漏完成：未覆盖 {} 项 / 新增建议 {} 条",
        sum(1 for v in coverage.values() if v == "未覆盖"), len(additions),
    )
    return {"coverage": coverage, "additions": additions, "model_name": model_name, "batches": len(batches)}


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
    llm: LLMClient, samples: list[dict], model: str | None
) -> list[dict]:
    """从人工修改留痕提炼规则候选；样本不足或无模式时返回空列表。"""
    if not samples:
        return []
    data, _ = await _chat_json(
        llm,
        [
            {"role": "system", "content": prompt_text("learning")},
            {"role": "user", "content": f"人工修改留痕样本（共 {len(samples)} 条）：\n{wrap_data('修改留痕', _dump(samples))}"},
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
