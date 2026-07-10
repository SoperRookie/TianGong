"""Prompt 实测脚本（POC-R9）：用真实模型跑完整三角色链路并输出质量观测数据。

用法：
    .venv/bin/python scripts/prompt_poc.py                              # 内置样例需求
    .venv/bin/python scripts/prompt_poc.py --file path/to/需求.md        # 指定需求文件
    .venv/bin/python scripts/prompt_poc.py --reviewer deepseek-reasoner  # 指定评审模型
"""

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents import run_generation
from app.config import get_settings
from app.exporters import export_csv, export_excel, export_xmind
from app.llm.client import LLMClient
from app.llm.registry import ModelRegistry
from app.parsers import parse_file, parse_text

SAMPLE_REQUIREMENT = """# 限时充值活动需求

## 活动规则
1. 活动期间（7 天）玩家单笔充值满 100 元，赠送 10% 元宝；满 500 元赠送 15%；满 1000 元赠送 20%。
2. 赠送元宝每日上限 5000，超出部分不赠送；次日 0 点重置。
3. 每位玩家活动期间累计充值满 2000 元，额外获得限定坐骑「赤焰麒麟」（全服唯一款式，活动结束后不再发放）。
4. 坐骑通过邮件发放，邮件保留 30 天，过期未领取则失效。

## 充值渠道
- 支持微信支付、支付宝、Apple 内购三种渠道。
- Apple 内购因平台抽成，赠送比例统一按微信/支付宝档位的 80% 计算，向下取整。

## 异常与边界
- 充值成功但回调延迟超过 5 分钟，客户端展示「充值处理中」，元宝到账后推送提醒。
- 活动结束瞬间发起的充值：以支付平台回调时间为准，回调时间在活动结束后则不参与活动。
- 退款处理：已参与活动的充值发生退款，扣回赠送元宝；若元宝已消耗导致余额不足，扣为负数并禁止消费直至补足。
"""


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default=None, help="需求文件路径，缺省用内置样例")
    parser.add_argument("--model", default=None, help="生成/拆解模型")
    parser.add_argument("--reviewer", default=None, help="评审模型（缺省与生成同模型）")
    args = parser.parse_args()

    settings = get_settings()
    registry = ModelRegistry.from_yaml(settings.models_config_path)
    llm = LLMClient(registry)

    if args.file:
        doc = parse_file(args.file)
        requirement, source = doc.full_text, doc.source
    else:
        requirement, source = parse_text(SAMPLE_REQUIREMENT).full_text, "内置样例（充值活动）"

    gen_model = args.model or registry.default_model
    print(f"需求来源: {source}")
    print(f"生成模型: {gen_model} | 评审模型: {args.reviewer or gen_model + '（同模型）'}")
    print("开始生成 ...\n")

    start = time.monotonic()
    result = await run_generation(
        requirement, llm=llm, model=args.model, reviewer_model=args.reviewer
    )
    elapsed = time.monotonic() - start

    # ---- 质量观测数据 ----
    priorities = Counter(c.priority for c in result.cases)
    modules = Counter(c.module for c in result.cases)
    step_counts = [len(c.steps) for c in result.cases]

    print(f"耗时: {elapsed:.1f}s | 评审轮数: {result.review_rounds} | 评审通过: {result.passed}")
    print(f"用例总数: {len(result.cases)}")
    print(f"优先级分布: {dict(sorted(priorities.items()))}")
    print(f"模块分布: {dict(modules)}")
    if step_counts:
        print(f"步骤数: 最少 {min(step_counts)} / 最多 {max(step_counts)} / 平均 {sum(step_counts) / len(step_counts):.1f}")
    if result.blind_spots:
        print(f"\n疑似需求盲区:\n" + "\n".join(f"  - {b}" for b in result.blind_spots))
    if result.missing:
        print(f"\n评审提示遗漏场景:\n" + "\n".join(f"  - {m}" for m in result.missing))
    if result.suggestions:
        print(f"\n评审优化建议（非阻断）:\n" + "\n".join(f"  - {s}" for s in result.suggestions))
    if result.unresolved:
        print(f"\n⚠ 未解决评审问题:\n" + "\n".join(f"  - [{i['case_id']}] {i['problem']}" for i in result.unresolved))

    print("\nAgent 调用链路:")
    for step in result.trace:
        print(f"  {json.dumps(step, ensure_ascii=False)}")

    out_dir = Path("outputs/prompt_poc")
    if result.cases:
        export_excel(result.cases, out_dir / "poc用例.xlsx")
        export_csv(result.cases, out_dir / "poc用例.csv")
        export_xmind(result.cases, out_dir / "poc用例.xmind", root_title=source)
        print(f"\n已导出: {out_dir}/poc用例.xlsx / .csv / .xmind")

    print("\n---- 全部用例预览 ----")
    for c in result.cases:
        print(f"[{c.priority}] {c.case_id} {c.title}（{len(c.steps)} 步）")


if __name__ == "__main__":
    asyncio.run(main())
