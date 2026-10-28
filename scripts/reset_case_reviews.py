"""把一个任务的全部用例审核状态重置为未审核（pending）：走在线评审接口的 unlock 动作，与页面上逐条「解锁」等价。

任务数据在服务进程里有内存缓存、写穿到 MySQL，直接改库会被下一次落库覆盖，所以必须走接口。
用法（在任何能访问服务的机器上，只依赖标准库）：
    python scripts/reset_case_reviews.py --base https://tiangong.example.com --task cc20fddc02ce --user admin
密码从环境变量 TIANGONG_PASSWORD 读，没有则交互输入；绑定了两步验证的账号加 --otp 动态码。
--dry-run 只统计不改。草稿（draft）状态的用例不动（那是人工新增还没提交的）。
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.request


def fetch(base: str, path: str, token: str | None = None, body: dict | None = None, method: str | None = None) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise SystemExit(f"{method or 'POST'} {path} → {e.code}: {detail[:500]}")


def plan_items(task: dict) -> tuple[list[str], dict[str, int]]:
    """要解锁的用例编号（状态不是 pending 或被锁定的，草稿不动）与按状态的统计。"""
    reviews = task.get("case_reviews") or {}
    counts: dict[str, int] = {}
    targets: list[str] = []
    for case in (task.get("result") or {}).get("cases") or []:
        state = reviews.get(str(case.get("uid") or "")) or {}
        status = state.get("status") or "pending"
        counts[status] = counts.get(status, 0) + 1
        if status == "draft":
            continue
        if status != "pending" or state.get("locked"):
            targets.append(str(case.get("case_id")))
    return targets, counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", required=True, help="服务地址，如 https://tiangong.example.com 或 http://127.0.0.1:8000")
    ap.add_argument("--task", required=True, help="任务 ID")
    ap.add_argument("--user", required=True)
    ap.add_argument("--otp", default=None, help="两步验证动态码")
    ap.add_argument("--batch", type=int, default=500, help="每次请求解锁的条数（接口整批原子）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    password = os.environ.get("TIANGONG_PASSWORD") or getpass.getpass(f"{args.user} 的密码：")
    login = fetch(args.base, "/api/v1/auth/login", body={"username": args.user, "password": password, "otp": args.otp})
    if login.get("otp_required"):
        raise SystemExit("该账号绑定了两步验证，请加 --otp 动态码")
    token = login["token"]

    task = fetch(args.base, f"/api/v1/tasks/{args.task}", token)
    targets, counts = plan_items(task)
    total = len((task.get("result") or {}).get("cases") or [])
    print(f"任务 {args.task}「{task.get('title') or ''}」共 {total} 条用例，当前状态：" +
          "、".join(f"{k} {v}" for k, v in sorted(counts.items())))
    if not targets:
        print("没有需要重置的用例。")
        return
    print(f"将把 {len(targets)} 条重置为未审核" + ("（dry-run，不改）" if args.dry_run else ""))
    if args.dry_run:
        return
    done = 0
    for i in range(0, len(targets), args.batch):
        chunk = targets[i:i + args.batch]
        fetch(args.base, f"/api/v1/tasks/{args.task}/review", token,
              body={"items": [{"case_id": cid, "action": "unlock"} for cid in chunk]})
        done += len(chunk)
        print(f"  已重置 {done}/{len(targets)}", flush=True)
    task = fetch(args.base, f"/api/v1/tasks/{args.task}", token)
    _, counts = plan_items(task)
    print("完成，当前状态：" + "、".join(f"{k} {v}" for k, v in sorted(counts.items())))
    fetch(args.base, "/api/v1/auth/logout", token, body={})


if __name__ == "__main__":
    sys.exit(main())
