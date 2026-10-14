"""需求分析评测集与回归评测：把「准确率」落成可反复测量的数字。

- 评测样本：一份需求原文 + 人工确认的标准答案（10 个分析项逐条文本）。来源：需求中心把人工修改 / 确认后的
  分析结果一键加入（同一需求重复加入即更新），或在评测页手工修订标准答案；
- 回归评测：对样本重新跑完整分析流水线（原文核验 + 独立复核），由裁判 Agent 把 AI 输出与标准答案逐条语义对齐，
  统计 匹配 / 理解错 / 捏造 / 遗漏，得出 准确率、捏造率、理解错率、遗漏率；每次改 Prompt、换模型后跑一遍即可对比；
- 结果按次留档（模型、复核模型、Prompt 版本、逐样本逐项差异），后台执行，页面轮询进度。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from loguru import logger

from app.agents.analysis import ITEM_KEYS, item_text, normalize
from app.agents.prompts import REQUIREMENT_ANALYSIS_LABELS, wrap_data
from app.db import DocStore

METRIC_LABELS = {"accuracy": "准确率", "fabricated_rate": "捏造率", "wrong_rate": "理解错率", "missing_rate": "遗漏率"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def golden_from_analysis(analysis: dict) -> dict[str, list[str]]:
    """分析结果 → 标准答案（只取文本；带未处理复核质疑的条目不进标准答案）。"""
    out: dict[str, list[str]] = {}
    for key in ITEM_KEYS:
        texts = []
        for x in analysis.get(key) or []:
            review = x.get("review") if isinstance(x, dict) else None
            if review and review.get("verdict") in ("wrong", "unsupported"):
                continue
            t = item_text(x)
            if t and t not in texts:
                texts.append(t)
        out[key] = texts
    return out


def empty_metrics() -> dict:
    return {"golden": 0, "predicted": 0, "matched": 0, "wrong": 0, "extra": 0, "missing": 0,
            "accuracy": None, "fabricated_rate": None, "wrong_rate": None, "missing_rate": None}


def finalize_metrics(m: dict) -> dict:
    """由计数算比率：准确率 = 匹配 / (标准答案 + 捏造)；捏造率 = 捏造 / AI 输出；理解错率 = 理解错 / (匹配 + 理解错)；遗漏率 = 遗漏 / 标准答案。"""
    def rate(a: int, b: int) -> float | None:
        return round(a / b, 4) if b else None

    m["accuracy"] = rate(m["matched"], m["golden"] + m["extra"])
    m["fabricated_rate"] = rate(m["extra"], m["predicted"])
    m["wrong_rate"] = rate(m["wrong"], m["matched"] + m["wrong"])
    m["missing_rate"] = rate(m["missing"], m["golden"])
    return m


def score_alignment(golden: dict[str, list[str]], predicted: dict[str, list[str]], verdict: dict) -> tuple[dict, dict]:
    """把裁判输出（编号对齐）折算成计数。裁判没提到的标准答案按遗漏、没提到的 AI 输出按捏造计（裁判必须逐条表态）。
    返回 (总计, 按分析项明细)。"""
    total = empty_metrics()
    per_key: dict[str, dict] = {}
    for key in ITEM_KEYS:
        g, p = golden.get(key) or [], predicted.get(key) or []
        v = verdict.get(key) if isinstance(verdict.get(key), dict) else {}
        matched_g: set[int] = set()
        used_p: set[int] = set()
        wrong: list[list] = []
        for pair in v.get("matched") or []:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                try:
                    gi, pi = int(pair[0]) - 1, int(pair[1]) - 1
                except (TypeError, ValueError):
                    continue
                if 0 <= gi < len(g) and 0 <= pi < len(p):
                    matched_g.add(gi)
                    used_p.add(pi)
        wrong_g: set[int] = set()
        for pair in v.get("wrong") or []:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                try:
                    gi, pi = int(pair[0]) - 1, int(pair[1]) - 1
                except (TypeError, ValueError):
                    continue
                if 0 <= gi < len(g) and 0 <= pi < len(p) and gi not in matched_g:
                    wrong_g.add(gi)
                    used_p.add(pi)
                    wrong.append([g[gi], p[pi], str(pair[2]) if len(pair) > 2 else ""])
        extra = [p[i] for i in range(len(p)) if i not in used_p]
        missing = [g[i] for i in range(len(g)) if i not in matched_g and i not in wrong_g]
        m = empty_metrics()
        m.update(golden=len(g), predicted=len(p), matched=len(matched_g), wrong=len(wrong_g), extra=len(extra), missing=len(missing))
        per_key[key] = {**finalize_metrics(m), "wrong_items": wrong, "extra_items": extra, "missing_items": missing}
        for k in ("golden", "predicted", "matched", "wrong", "extra", "missing"):
            total[k] += m[k]
    return finalize_metrics(total), per_key


def _listing(data: dict[str, list[str]]) -> str:
    parts = []
    for key in ITEM_KEYS:
        items = data.get(key) or []
        parts.append(f"## {key}（{REQUIREMENT_ANALYSIS_LABELS[key]}）\n" + ("\n".join(f"{i + 1}. {t}" for i, t in enumerate(items)) or "（空）"))
    return "\n\n".join(parts)


async def judge_sample(llm, golden: dict, predicted: dict, model: str | None) -> tuple[dict, dict, str]:
    """裁判 Agent 对齐：返回 (总计, 按项明细, 裁判模型)。标准答案与 AI 输出全空时直接满分不调模型。"""
    from app.agents.graph import _chat_json
    from app.prompts import prompt_text

    if not any(golden.values()) and not any(predicted.values()):
        total, per_key = score_alignment(golden, predicted, {})
        return total, per_key, ""
    # 规范化后完全相同的条目先行判定为匹配，交给裁判的只剩需要语义判断的
    pre: dict = {}
    for key in ITEM_KEYS:
        gn = {normalize(t): i + 1 for i, t in enumerate(golden.get(key) or [])}
        pairs = [[gn[normalize(t)], j + 1] for j, t in enumerate(predicted.get(key) or []) if normalize(t) in gn]
        if pairs:
            pre[key] = pairs
    user = (wrap_data("标准答案", _listing(golden)) + "\n\n" + wrap_data("AI 输出", _listing(predicted))
            + ("\n\n以下编号对已由程序判定为完全相同，直接作为 matched 输出即可：" + str(pre) if pre else ""))
    data, result = await _chat_json(
        llm, [{"role": "system", "content": prompt_text("analysis_judge")}, {"role": "user", "content": user}],
        model, reasoning_effort="high")
    for key, pairs in pre.items():  # 程序判定的匹配优先于裁判（裁判漏报也不丢）
        sec = data.get(key)
        if not isinstance(sec, dict):
            sec = data[key] = {}
        seen = {tuple(x[:2]) for x in sec.get("matched") or [] if isinstance(x, (list, tuple)) and len(x) >= 2}
        sec["matched"] = [*(sec.get("matched") or []), *[pr for pr in pairs if tuple(pr) not in seen]]
    total, per_key = score_alignment(golden, predicted, data)
    return total, per_key, result.model_name


class AnalysisEvalStore:
    def __init__(self):
        self._samples_doc = DocStore("analysis_eval_samples")
        self._runs_doc = DocStore("analysis_eval_runs")
        self._samples: dict[str, dict] = self._samples_doc.load_all()
        self._runs: dict[str, dict] = self._runs_doc.load_all()

    # ---- 样本 ----

    def list_samples(self, project: str | None = None) -> list[dict]:
        items = [s for s in self._samples.values() if project is None or s.get("project") == project]
        return sorted(items, key=lambda s: s.get("updated_at") or "", reverse=True)

    def get_sample(self, sample_id: str) -> dict | None:
        return self._samples.get(sample_id)

    def upsert_from_requirement(self, item: dict, text: str, by: str | None, origin: str = "manual") -> dict:
        """需求的当前分析结果（人工修改 / 确认后）作为标准答案；同一需求重复加入即更新（标准答案以最新为准）。"""
        from app.requirements import RequirementError

        if not item.get("analysis"):
            raise RequirementError("还没有分析结果，不能作为标准答案")
        golden = golden_from_analysis(item["analysis"])
        if not any(golden.values()):
            raise RequirementError("分析结果为空，不能作为标准答案")
        sid = f"req-{item['req_id']}"
        old = self._samples.get(sid) or {}
        sample = {
            "sample_id": sid, "req_id": item["req_id"], "project": item.get("project"),
            "title": item.get("title") or item["req_id"], "text": text, "golden": golden,
            "origin": origin, "edits": len(item.get("analysis_edits") or []),
            "analysis_model": (item.get("analysis_meta") or {}).get("model"),
            "notes": old.get("notes", ""), "created_by": old.get("created_by") or by, "created_at": old.get("created_at") or _now(),
            "updated_by": by, "updated_at": _now(),
        }
        self._samples[sid] = sample
        self._samples_doc.put(sid, sample)
        return sample

    def update_sample(self, sample_id: str, golden: dict | None = None, title: str | None = None,
                      notes: str | None = None, by: str | None = None) -> dict:
        sample = self._samples.get(sample_id)
        if sample is None:
            raise KeyError(sample_id)
        if golden is not None:
            cleaned = {k: [str(t).strip() for t in (golden.get(k) or []) if str(t).strip()] for k in ITEM_KEYS}
            if not any(cleaned.values()):
                raise ValueError("标准答案不能全空")
            sample["golden"] = cleaned
        if title is not None and title.strip():
            sample["title"] = title.strip()
        if notes is not None:
            sample["notes"] = notes.strip()
        sample["updated_by"], sample["updated_at"] = by, _now()
        self._samples_doc.put(sample_id, sample)
        return sample

    def delete_sample(self, sample_id: str) -> bool:
        if self._samples.pop(sample_id, None) is None:
            return False
        self._samples_doc.remove(sample_id)
        return True

    # ---- 评测运行 ----

    def list_runs(self, limit: int = 50) -> list[dict]:
        runs = sorted(self._runs.values(), key=lambda r: r.get("at") or "", reverse=True)[:limit]
        return [{k: v for k, v in r.items() if k != "samples"} | {"sample_count": len(r.get("samples") or [])} for r in runs]

    def get_run(self, run_id: str) -> dict | None:
        return self._runs.get(run_id)

    def running(self) -> dict | None:
        return next((r for r in self._runs.values() if r.get("status") == "running"), None)

    def create_run(self, samples: list[dict], by: str | None, model: str | None, reviewer_model: str | None,
                   review: bool, project: str | None, prompt_versions: dict | None = None) -> dict:
        run = {
            "run_id": uuid.uuid4().hex[:10], "status": "running", "by": by, "at": _now(), "finished_at": None,
            "project": project, "model": model, "reviewer_model": reviewer_model, "review": review,
            "prompt_versions": prompt_versions or {}, "total": len(samples), "done": 0, "failed": 0,
            "samples": [{"sample_id": s["sample_id"], "title": s["title"], "project": s.get("project"), "status": "pending"} for s in samples],
            "totals": None, "per_key": None, "error": None,
        }
        self._runs[run["run_id"]] = run
        self._runs_doc.put(run["run_id"], run)
        return run

    def save_run(self, run: dict) -> None:
        self._runs_doc.put(run["run_id"], run)

    def mark_interrupted(self) -> int:
        """服务重启：仍是 running 的评测标为失败（后台任务随进程消失）。"""
        n = 0
        for run in self._runs.values():
            if run.get("status") == "running":
                run.update(status="failed", error="服务重启导致评测中断", finished_at=_now())
                self._runs_doc.put(run["run_id"], run)
                n += 1
        return n


def aggregate(samples: list[dict]) -> tuple[dict, dict]:
    """逐样本结果汇总：总计与按分析项。"""
    total = empty_metrics()
    per_key = {k: empty_metrics() for k in ITEM_KEYS}
    for s in samples:
        if s.get("status") != "done":
            continue
        for k in ("golden", "predicted", "matched", "wrong", "extra", "missing"):
            total[k] += (s.get("metrics") or {}).get(k, 0)
            for key in ITEM_KEYS:
                per_key[key][k] += ((s.get("per_key") or {}).get(key) or {}).get(k, 0)
    return finalize_metrics(total), {k: finalize_metrics(v) for k, v in per_key.items()}


def _set_profile(app, project: str | None) -> None:
    """项目画像进入 Agent 上下文（与 routes._set_profile_context 同样逻辑；故障降级为无画像）。"""
    from app.agents.context import set_profile
    from app.config import get_settings

    try:
        desc = (app.state.projects.get(project) or {}).get("description", "") if project else ""
        set_profile(app.state.profiles.render(project, get_settings().profile_budget_chars, description=desc))
    except Exception as e:
        logger.warning("评测加载项目画像失败，降级为无画像：{}", e)
        set_profile(None)


async def run_eval(app, run_id: str, knowledge_fn=None) -> None:
    """后台执行一次评测：逐样本跑分析流水线 → 裁判对齐 → 计分；串行执行避免并发打爆模型配额。"""
    from app.agents.analysis import run_requirement_analysis
    from app.llm.calllog import set_ai_context

    store: AnalysisEvalStore = app.state.analysis_eval
    run = store.get_run(run_id)
    if run is None:
        return
    for entry in run["samples"]:
        sample = store.get_sample(entry["sample_id"])
        if sample is None:
            entry.update(status="failed", error="样本已删除")
            run["failed"] += 1
            continue
        try:
            set_ai_context(project=sample.get("project"), requirement_id=sample.get("req_id"), by=run.get("by"), purpose="需求分析评测")
            _set_profile(app, sample.get("project"))   # 与线上分析同样带项目画像，评出来的数字才可比
            refs = None
            if knowledge_fn is not None:
                try:
                    refs = await knowledge_fn(app, sample["text"], sample.get("project"), sample.get("req_id"))
                except Exception as e:
                    logger.warning("评测知识注入失败，无知识继续：{}", e)
            analysis = await run_requirement_analysis(
                sample["text"], app.state.llm, run.get("model"), knowledge_refs=refs,
                review=run.get("review"), reviewer_model=run.get("reviewer_model"))
            predicted = {k: [item_text(x) for x in analysis.get(k) or [] if item_text(x)] for k in ITEM_KEYS}
            metrics, per_key, judge_model = await judge_sample(app.state.llm, sample["golden"], predicted, run.get("model"))
            entry.update(status="done", metrics=metrics, per_key=per_key, predicted=predicted, golden=sample["golden"],
                         model=analysis.get("model_name"), judge_model=judge_model,
                         verification=analysis.get("verification"), review=analysis.get("review"),
                         open_questions=[q.get("question") if isinstance(q, dict) else str(q) for q in analysis.get("open_questions") or []])
            run["done"] += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("评测样本失败（{}）：{}", entry["sample_id"], e)
            entry.update(status="failed", error=str(e)[:300])
            run["failed"] += 1
        store.save_run(run)
    run["totals"], run["per_key"] = aggregate(run["samples"])
    run["status"] = "done" if run["done"] else "failed"
    if not run["done"]:
        run["error"] = "没有样本评测成功"
    run["finished_at"] = _now()
    store.save_run(run)
    t = run["totals"]
    logger.info("需求分析评测完成（{}）：{} 个样本，准确率 {}，捏造率 {}，理解错率 {}，遗漏率 {}",
                run_id, run["done"], t["accuracy"], t["fabricated_rate"], t["wrong_rate"], t["missing_rate"])
