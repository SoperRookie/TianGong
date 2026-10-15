"""解析结果体检：解析出来的文本几乎全是代码 / 样式 / 乱码时给出告警，避免直接送去分析与拆解（烧掉几百次模型调用后才发现喂的是源码）。"""

from __future__ import annotations

import re

MIN_CHARS = 1500                 # 太短的不判（标题、几行说明）
CJK_RATIO_MIN = 0.05             # 中文占比低于此值才可能是非需求文本
CODE_LINE_RATIO = 0.3            # 代码 / 样式类行占比
_CODE_LINE_RE = re.compile(r"^\s*(?:[#.\w\-\[\]:>,\s]+\{|\}|[\w\-]+\s*:\s*[^;{}]+;|(?:var|let|const|function|return|import|export)\b|[\w$.]+\s*\(.*\)\s*[;{]?\s*$|\$axure|</?\w+[^>]*>)")
# cp437 把 GBK / UTF-8 文件名解出来的典型字符：Latin-1 重音字母、希腊字母、制表符 / 方块（如 Φ┐₧Φ╡óµîæµêÿ），连续 3 个以上即视为乱码串
_MOJIBAKE_RE = re.compile(r"[\u00a1-\u00ff\u0391-\u03c9\u2500-\u25a0\u20a7\u0192\u207f\u2219\u221a\u221e\u2229\u2248\u2261\u2264\u2265\u2310\u2320\u2321]{3,}")


def content_warning(text: str) -> str | None:
    """返回告警文案；正常返回 None。"""
    if not text or len(text) < MIN_CHARS:
        return None
    total = len(text)
    cjk = len(re.findall(r"[一-鿿]", text))
    lines = [ln for ln in text.splitlines() if ln.strip()]
    code = sum(1 for ln in lines if _CODE_LINE_RE.match(ln))
    code_ratio = code / len(lines) if lines else 0.0
    cjk_ratio = cjk / total
    moji = len(_MOJIBAKE_RE.findall(text))
    reasons = []
    if cjk_ratio < CJK_RATIO_MIN and code_ratio >= CODE_LINE_RATIO:
        reasons.append(f"中文占比 {cjk_ratio:.0%}，代码 / 样式类行占比 {code_ratio:.0%}，像是网页或原型导出的脚本样式源码")
    elif cjk_ratio < CJK_RATIO_MIN and moji >= 5:
        reasons.append(f"中文占比 {cjk_ratio:.0%} 且出现 {moji} 处乱码串，压缩包文件名或编码可能有问题")
    elif cjk_ratio < CJK_RATIO_MIN and total > 50000:
        reasons.append(f"中文占比 {cjk_ratio:.0%}（{total} 字），请确认这是需求正文而不是导出的资源文件")
    if not reasons:
        return None
    return "疑似非需求文本：" + "；".join(reasons) + "。建议改传 Word / PDF / 截图或 Axure 导出包，或人工确认后再分析"
