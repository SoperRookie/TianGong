"""测试报告 PDF（带图表）：结论 → 指标 → 执行结果构成 → 按优先级 / 按模块分布 → 失败与阻塞分类 → 各轮次通过率
→ 风险 / 遗留 / 建议 → 失败与阻塞用例清单。

用 PyMuPDF 直接画（矢量，中文用内置 CJK 字体，不依赖服务器装字体，也不依赖浏览器打印），
版式与页面「测试报告」页签的图表一致：同一份程序统计 report.stats，同一套状态色。
"""
from __future__ import annotations

try:  # PyMuPDF 新版包名是 pymupdf，旧版是 fitz
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz

W, H = 595.0, 842.0          # A4 竖版（pt）
M = 40.0                     # 页边距
CW = W - 2 * M               # 内容宽
FONT = "china-s"             # 内置简体中文字体（Droid Sans Fallback），含拉丁字符
FONT_REF = "tg"              # 嵌入页面后的字体名


def _rgb(hex_: str) -> tuple[float, float, float]:
    h = hex_.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


INK, INK2, MUTED, LINE, PANEL = _rgb("#202941"), _rgb("#4b5970"), _rgb("#8b98ad"), _rgb("#e1e6ed"), _rgb("#f6f8fb")
STATES = [  # key, 名称, 颜色（与页面图表一致，通过 / 失败经色弱可分性校验）
    ("pass", "通过", _rgb("#2aa784")), ("fail", "失败", _rgb("#c8323f")), ("blocked", "阻塞", _rgb("#f0a03d")),
    ("skipped", "跳过", _rgb("#9aa6b8")), ("not_executed", "未执行", _rgb("#dfe5ee")),
]
VERDICT = {"可发布": _rgb("#2aa784"), "不可发布": _rgb("#c8323f")}
ACCENT, GRAY_BAR, BLUE_BAR = _rgb("#1f6fe5"), _rgb("#c3ccda"), _rgb("#5b7fd6")


def _n(o: dict | None, k: str) -> int:
    try:
        return int((o or {}).get(k) or 0)
    except (TypeError, ValueError):
        return 0


def _pct(a: int, b: int) -> str:
    return f"{round(1000 * a / b) / 10:g}%" if b else "—"


def _executed(o: dict) -> int:
    return _n(o, "total") - _n(o, "not_executed")


def _pass_rate(o: dict) -> str:
    return _pct(_n(o, "pass"), _executed(o))   # 通过 / 已执行，与 run_summary 同口径


class _Canvas:
    """极简排版：一个向下走的游标，放不下就换页；文字按字符宽度折行（中文没有空格，不能按词折）。"""

    def __init__(self, footer: str):
        self.doc = fitz.open()
        self.font = fitz.Font(FONT)
        self.footer = footer
        self._adv: dict[str, float] = {}
        self.page = None
        self.y = M
        self.new_page()

    def new_page(self) -> None:
        self.page = self.doc.new_page(width=W, height=H)
        # 按字体文件嵌入而不是直接用内置 CJK 名：后者会把英文数字也排成全角宽度，量出来的宽度和画出来的对不上
        self.page.insert_font(fontname=FONT_REF, fontbuffer=self.font.buffer)
        self.y = M

    def ensure(self, h: float) -> None:
        if self.y + h > H - M - 14:
            self.new_page()

    # ---- 文字 ----
    def width(self, s: str, size: float) -> float:
        total = 0.0
        for ch in s:
            a = self._adv.get(ch)
            if a is None:
                a = self._adv[ch] = self.font.glyph_advance(ord(ch))
            total += a
        return total * size

    def wrap(self, s: str, size: float, width: float) -> list[str]:
        out: list[str] = []
        for para in str(s or "").replace("\r", "").split("\n"):
            line, w = "", 0.0
            for ch in para:
                cw = self.width(ch, size)
                if line and w + cw > width:
                    # 行首不留收尾标点：把它并到上一行
                    if ch in "，。；：、）》」』！？,.;:)]%":
                        line += ch
                        out.append(line)
                        line, w = "", 0.0
                        continue
                    out.append(line)
                    line, w = "", 0.0
                line += ch
                w += cw
            out.append(line)
        return out or [""]

    def fit(self, s: str, size: float, width: float) -> str:
        """单行放不下时截断加省略号。"""
        s = " ".join(str(s or "").split())
        if self.width(s, size) <= width:
            return s
        while s and self.width(s + "…", size) > width:
            s = s[:-1]
        return s + "…"

    def text(self, x: float, y: float, s: str, size: float = 9.5, color=INK, bold: bool = False, align: str = "left", width: float = 0) -> None:
        """y 是文字顶边；align=right / center 时在 [x, x+width] 内对齐。"""
        s = str(s)
        if align != "left" and width:
            tw = self.width(s, size)
            x = x + width - tw if align == "right" else x + (width - tw) / 2
        kw = {"render_mode": 2, "border_width": 0.035, "fill": color} if bold else {}   # 这套字体没有粗体：同色描边加粗
        self.page.insert_text((x, y + size * 0.86), s, fontname=FONT_REF, fontsize=size, color=color, **kw)

    def para(self, x: float, s: str, size: float = 9.5, color=INK, width: float = CW, lead: float = 1.5, bold: bool = False) -> None:
        """从游标处往下排一段自动折行的文字，逐行检查换页。"""
        for line in self.wrap(s, size, width):
            self.ensure(size * lead)
            self.text(x, self.y, line, size, color, bold)
            self.y += size * lead

    # ---- 图形 ----
    def rect(self, x: float, y: float, w: float, h: float, fill, radius: float = 0.0, stroke=None) -> None:
        if w <= 0 or h <= 0:
            return
        r = fitz.Rect(x, y, x + w, y + h)
        kw = {"radius": min(0.5, radius / min(w, h))} if radius and min(w, h) > 0 else {}
        try:
            self.page.draw_rect(r, color=stroke, fill=fill, width=0.6 if stroke else 0, **kw)
        except (TypeError, ValueError):  # 老版本没有 radius 参数
            self.page.draw_rect(r, color=stroke, fill=fill, width=0.6 if stroke else 0)

    def hline(self, y: float, x0: float = M, x1: float = W - M, color=LINE) -> None:
        self.page.draw_line((x0, y), (x1, y), color=color, width=0.6)

    def segbar(self, x: float, y: float, w: float, h: float, o: dict, gap: float = 1.5, labels: bool = False) -> None:
        """分段条：各状态按条数占比，块之间留白分隔；labels=True 时在够宽的块里印百分比。"""
        total = _n(o, "total")
        parts = [(k, c, _n(o, k)) for k, _, c in STATES if _n(o, k) > 0]
        if not total or not parts:
            self.rect(x, y, w, h, STATES[-1][2], radius=min(3, h / 2))
            return
        avail = w - gap * (len(parts) - 1)
        cx = x
        for k, color, v in parts:
            pw = max(1.5, avail * v / total)
            self.rect(cx, y, pw, h, color, radius=min(3, h / 2))
            if labels:
                lab = _pct(v, total)
                if self.width(lab, 9) + 10 <= pw:
                    self.text(cx + 6, y + (h - 9) / 2, lab, 9, (1, 1, 1) if k in ("pass", "fail") else INK, bold=True)
            cx += pw + gap

    def section(self, title: str, sub: str = "", need: float = 60) -> None:
        """小节标题；need 是标题后至少要跟着的高度，放不下就整节换页，不把标题孤零零留在页尾。"""
        self.ensure(26 + need)
        self.y += 8
        self.text(M, self.y, title, 11.5, INK, bold=True)
        if sub:
            self.text(M + self.width(title, 11.5) + 8, self.y + 2, sub, 8.5, MUTED)
        self.y += 20

    def finish(self) -> bytes:
        n = len(self.doc)
        for i, page in enumerate(self.doc, 1):
            self.page = page
            self.hline(H - M + 4)
            self.text(M, H - M + 8, self.footer, 8, MUTED)
            self.text(M, H - M + 8, f"{i} / {n}", 8, MUTED, align="right", width=CW)
        try:
            self.doc.subset_fonts()   # 只留用到的字形，否则整套中文字体几 MB 都进文件
        except Exception:  # noqa: BLE001  子集化失败不影响内容，只是文件大一些
            pass
        data = self.doc.tobytes(deflate=True, garbage=3)
        self.doc.close()
        return data


def _legend(c: _Canvas, o: dict | None = None) -> None:
    """状态图例一行；传了 o 就带条数与占比。"""
    x = M
    for k, name, color in STATES:
        if o is not None and not _n(o, k):
            continue
        label = name if o is None else f"{name} {_n(o, k)} 条 · {_pct(_n(o, k), _n(o, 'total'))}"
        c.rect(x, c.y + 1.5, 7, 7, color, radius=1.5)
        c.text(x + 11, c.y, label, 8.5, INK2)
        x += 11 + c.width(label, 8.5) + 16
    c.y += 16


def _stat_rows(c: _Canvas, rows: list[dict], name_w: float) -> None:
    """对齐的分段条列表：名称 | 条 | 通过率 + 条数。"""
    val_w = 150.0
    bar_x, bar_w = M + name_w + 8, CW - name_w - 8 - val_w - 8
    for r in rows:
        c.ensure(17)
        c.text(M, c.y + 2, c.fit(r["name"], 9, name_w), 9, INK)
        c.segbar(bar_x, c.y + 4, bar_w, 7, r)
        extra = f"{_n(r, 'total')} 条" + (f" · 失败 {_n(r, 'fail')}" if _n(r, "fail") else "") + (f" · 阻塞 {_n(r, 'blocked')}" if _n(r, "blocked") else "")
        c.text(bar_x + bar_w + 8, c.y + 2, _pass_rate(r), 9, INK, bold=True)
        c.text(bar_x + bar_w + 8, c.y + 2.5, extra, 8, MUTED, align="right", width=val_w)
        c.y += 17


def _rank_rows(c: _Canvas, rows: list[dict], name_w: float, single=None) -> None:
    """排序条：失败 / 阻塞两段（single 给定时整条一个颜色）。"""
    mx = max([_n(r, "fail") + _n(r, "blocked") for r in rows] or [1]) or 1
    val_w = 120.0
    bar_x, bar_w = M + name_w + 8, CW - name_w - 8 - val_w - 8
    for r in rows:
        c.ensure(17)
        f, b = _n(r, "fail"), _n(r, "blocked")
        c.text(M, c.y + 2, c.fit(r.get("name") or "—", 9, name_w), 9, INK)
        full = bar_w * (f + b) / mx
        if single:
            c.rect(bar_x, c.y + 4, max(2, full), 7, single, radius=3)
        else:
            fw = full * f / (f + b) if f + b else 0
            c.rect(bar_x, c.y + 4, fw - (1.5 if f and b else 0), 7, STATES[1][2], radius=3)
            c.rect(bar_x + fw, c.y + 4, full - fw, 7, STATES[2][2], radius=3)
        c.text(bar_x + bar_w + 8, c.y + 2, str(f + b), 9, INK, bold=True)
        detail = "条" if single else (f"失败 {f} · 阻塞 {b}" if f and b else ("失败" if f else "阻塞"))
        c.text(bar_x + bar_w + 8, c.y + 2.5, detail, 8, MUTED, align="right", width=val_w)
        c.y += 17


def _bullets(c: _Canvas, title: str, items: list[tuple[str, str]], tag_colors: dict | None = None) -> None:
    """带标签的条目列表：[(标签, 正文)]，正文自动折行。"""
    c.section(f"{title} · {len(items)}", need=30)
    if not items:
        c.text(M, c.y, "无", 9.5, MUTED)
        c.y += 16
        return
    for tag, body in items:
        tw = c.width(tag, 8) + 10
        lines = c.wrap(body, 9.5, CW - tw - 8)
        c.ensure(len(lines[:2]) * 14.5 + 4)
        color = (tag_colors or {}).get(tag, INK2)
        c.rect(M, c.y, tw, 13, PANEL, radius=2.5, stroke=LINE)
        c.text(M + 5, c.y + 2.2, tag, 8, color)
        for line in lines:
            c.ensure(14.5)
            c.text(M + tw + 8, c.y + 1, line, 9.5, INK)
            c.y += 14.5
        c.y += 4


def build_report_pdf(scope: dict, report: dict, rounds: list[dict], failures: list[dict], generated_at: str = "") -> bytes:
    """scope：_report_inputs 的范围（plan / project / owner / period / run_name / round / modules…）；
    report：run.report（结论、判据、stats、risks…，可含 failure_breakdown）；
    rounds：[{name, executed, pass, current}] 各轮次；failures：失败与阻塞清单（导出时的当前结果）。"""
    stats = report.get("stats") or {}
    conclusion = report.get("conclusion") or "—"
    vcolor = VERDICT.get(conclusion, _rgb("#d68a28"))
    c = _Canvas(footer=f"天工 · 测试报告 · {scope.get('plan', '')} · {scope.get('run_name', '')}" + (f" · 导出于 {generated_at}" if generated_at else ""))

    # ---- 标题 ----
    c.text(M, c.y, "测试报告", 9.5, MUTED)
    c.y += 15
    c.para(M, scope.get("plan") or "（未命名计划）", 19, INK, lead=1.35, bold=True)
    c.y += 2
    meta = [f"轮次：{scope.get('run_name', '')}（第 {scope.get('round', 1)} 轮{'' if scope.get('finished') else '，进行中'}）",
            f"项目：{scope.get('project') or '—'}", f"负责人：{scope.get('owner') or '—'}", f"周期：{scope.get('period') or '—'}"]
    c.para(M, "    ".join(meta), 9, INK2, lead=1.6)
    gen = f"报告生成：{report.get('by') or '—'} · {str(report.get('at') or '')[:16].replace('T', ' ')} UTC" + (f" · {report['model']}" if report.get("model") else "")
    if report.get("interim"):
        gen += " · 阶段性报告（轮次未结束）"
    c.para(M, gen, 8.5, MUTED, lead=1.6)
    c.y += 6

    # ---- 结论 ----
    basis = c.wrap("判据：" + (report.get("verdict_basis") or "—"), 9.5, CW - 28)
    adjusted = c.wrap("程序校正：" + "；".join(report.get("adjusted") or []), 8.5, CW - 28) if report.get("adjusted") else []
    summary = c.wrap(report.get("summary") or "", 9.5, CW - 28) if report.get("summary") else []
    box_h = 14 + 24 + len(basis) * 14.5 + len(adjusted) * 13 + (6 + len(summary) * 14.5 if summary else 0) + 10
    c.ensure(box_h)
    c.rect(M, c.y, CW, box_h, PANEL, radius=4)
    c.rect(M, c.y, 4, box_h, vcolor)
    top = c.y
    c.y += 14
    c.text(M + 16, c.y, conclusion, 17, vcolor, bold=True)
    c.y += 24
    for line in basis:
        c.text(M + 16, c.y, line, 9.5, INK)
        c.y += 14.5
    for line in adjusted:
        c.text(M + 16, c.y, line, 8.5, _rgb("#a86414"))
        c.y += 13
    if summary:
        c.y += 6
        for line in summary:
            c.text(M + 16, c.y, line, 9.5, INK2)
            c.y += 14.5
    c.y = top + box_h + 12

    # ---- 指标卡 ----
    ex, total = _executed(stats), _n(stats, "total")
    tiles = [("通过率", _pass_rate(stats), f"通过 {_n(stats, 'pass')} / 已执行 {ex}", None),
             ("已执行", f"{ex} / {total}", f"执行率 {_pct(ex, total)}", None),
             ("失败", str(_n(stats, "fail")), f"占已执行 {_pct(_n(stats, 'fail'), ex)}", STATES[1][2]),
             ("阻塞", str(_n(stats, "blocked")), f"占已执行 {_pct(_n(stats, 'blocked'), ex)}", STATES[2][2]),
             ("未执行", str(_n(stats, "not_executed")), f"占全部 {_pct(_n(stats, 'not_executed'), total)}", STATES[4][2])]
    gap, th = 8.0, 70.0
    first_w = (CW - gap * 4) * 0.28
    rest_w = (CW - gap * 4 - first_w) / 4
    c.ensure(th + 10)
    x = M
    for i, (label, value, foot, dot) in enumerate(tiles):
        tw = first_w if i == 0 else rest_w
        c.rect(x, c.y, tw, th, None, radius=4, stroke=LINE)
        lx = x + 10
        if dot:
            c.rect(lx, c.y + 11.5, 6, 6, dot, radius=1.5)
            lx += 10
        c.text(lx, c.y + 10, label, 8.5, MUTED)
        c.text(x + 10, c.y + 25, value, 22 if i == 0 else 16, INK, bold=True)
        c.text(x + 10, c.y + th - 15, c.fit(foot, 8, tw - 16), 8, MUTED)
        x += tw + gap
    c.y += th + 6

    # ---- 执行结果构成 ----
    c.section("执行结果构成", f"共 {total} 条用例", need=50)
    c.segbar(M, c.y, CW, 22, stats, gap=2, labels=True)
    c.y += 30
    _legend(c, stats)

    # ---- 按优先级 / 按模块 ----
    prio = [{**p, "name": p.get("priority") or "（无优先级）"} for p in stats.get("by_priority") or []]
    if prio:
        c.section("按优先级", "分段条为各状态占比 · 右侧为通过率（通过 / 已执行）", need=17 * min(len(prio), 4) + 16)
        _stat_rows(c, prio, 70)
        c.y += 2
        _legend(c)
    mods = [{**m, "name": m.get("module") or "（未分模块）"} for m in stats.get("by_module") or []]
    mods.sort(key=lambda m: (-(_n(m, "fail") + _n(m, "blocked")), -_n(m, "total")))
    if mods:
        c.section("按模块", f"{len(mods)} 个模块 · 有失败或阻塞的排在前面", need=17 * min(len(mods), 4))
        _stat_rows(c, mods, 170)

    # ---- 失败与阻塞分类 ----
    bd = report.get("failure_breakdown") or {}
    reasons = bd.get("by_reason")
    live = reasons is None
    if live:   # 老报告没有快照：按导出时的当前结果
        acc: dict[str, dict] = {}
        for f in failures:
            b = acc.setdefault(f.get("reason") or "未填分类", {"name": f.get("reason") or "未填分类", "fail": 0, "blocked": 0})
            b["fail" if f.get("status") == "fail" else "blocked"] += 1
        reasons = sorted(acc.values(), key=lambda b: -(b["fail"] + b["blocked"]))
    c.section("失败与阻塞分类", "按导出时的当前结果统计" if live else "报告生成时的快照", need=40)
    if reasons:
        _rank_rows(c, reasons, 110)
        c.y += 2
        x = M
        for name, color in (("失败", STATES[1][2]), ("阻塞", STATES[2][2])):
            c.rect(x, c.y + 1.5, 7, 7, color, radius=1.5)
            c.text(x + 11, c.y, name, 8.5, INK2)
            x += 11 + c.width(name, 8.5) + 16
        c.y += 16
    else:
        c.text(M, c.y, "本轮没有失败或阻塞的用例", 9.5, MUTED)
        c.y += 16
    if bd.get("by_severity"):
        c.section("缺陷严重程度", "已出缺陷单的失败用例", need=40)
        _rank_rows(c, bd["by_severity"], 110, single=BLUE_BAR)

    # ---- 各轮次通过率 ----
    if len(rounds) > 1:
        plot_h = 96.0
        c.section("各轮次通过率", "通过 / 已执行 · 蓝色为本报告所在轮次", need=plot_h + 34)
        shown = rounds[-12:]
        slot = min(72.0, CW / len(shown))
        base = c.y + plot_h
        for i, r in enumerate(shown):
            cx = M + slot * i + slot / 2
            exd = _n(r, "executed")
            if exd:
                rate = _n(r, "pass") / exd
                bh = max(2.0, (plot_h - 16) * rate)
                c.rect(cx - 9, base - bh, 18, bh, ACCENT if r.get("current") else GRAY_BAR, radius=3)
                c.text(cx - slot / 2, base - bh - 12, _pct(_n(r, "pass"), exd), 8, INK2, align="center", width=slot)
            else:
                c.text(cx - slot / 2, base - 12, "—", 8, MUTED, align="center", width=slot)
            c.text(cx - slot / 2 + 2, base + 5, c.fit(r.get("name") or f"第 {i + 1} 轮", 8, slot - 4), 8, INK if r.get("current") else MUTED,
                   align="center", width=slot - 4, bold=bool(r.get("current")))
        c.hline(base, M, M + slot * len(shown), _rgb("#c9d1dd"))
        c.y = base + 22

    # ---- 风险 / 遗留 / 建议 ----
    _bullets(c, "风险", [(str(i), r.get("text", "") + (f"（影响：{r['impact']}）" if r.get("impact") else "")) for i, r in enumerate(report.get("risks") or [], 1)])
    _bullets(c, "遗留事项", [(o.get("kind") or "遗留", (f"{o['ref']} " if o.get("ref") else "") + o.get("text", "")) for o in report.get("open_items") or []])
    _bullets(c, "建议", [("可延后" if s.get("when") == "later" else "发布前必须", s.get("text", "")) for s in report.get("suggestions") or []],
             tag_colors={"发布前必须": _rgb("#b02a36")})

    # ---- 失败与阻塞用例清单 ----
    if failures:
        cap = 150
        c.section("失败与阻塞用例清单", f"导出时的当前结果 · 共 {len(failures)} 条" + (f"，列出前 {cap} 条" if len(failures) > cap else ""), need=60)
        cols = [("用例", 92), ("模块", 96), ("优先级", 34), ("状态", 30), ("失败分类", 70), ("标题 / 执行说明", CW - 92 - 96 - 34 - 30 - 70)]

        def head() -> None:
            c.rect(M, c.y, CW, 16, PANEL)
            x = M
            for name, w in cols:
                c.text(x + 4, c.y + 3.5, name, 8, INK2, bold=True)
                x += w
            c.y += 16

        head()
        for f in failures[:cap]:
            body = " ".join(str(f.get("title") or "").split()) + (f"｜{' '.join(str(f['note']).split())}" if f.get("note") else "")
            lines = c.wrap(body, 8.5, cols[5][1] - 8)[:3]
            rh = max(15.0, len(lines) * 12 + 4)
            if c.y + rh > H - M - 14:
                c.new_page()
                head()
            x = M
            cells = [f.get("ref") or "—", f.get("module") or "—", f.get("priority") or "—", "失败" if f.get("status") == "fail" else "阻塞", f.get("reason") or "—"]
            for (_, w), val in zip(cols[:5], cells):
                c.text(x + 4, c.y + 3, c.fit(val, 8.5, w - 8), 8.5, STATES[1][2] if val == "失败" else INK)
                x += w
            for i, line in enumerate(lines):
                c.text(x + 4, c.y + 3 + i * 12, line, 8.5, INK)
            c.y += rh
            c.hline(c.y, color=_rgb("#eef1f6"))
    return c.finish()
