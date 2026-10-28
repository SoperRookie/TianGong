"""把离线改好的用例文件回传到任务（概览页「离线终稿回传」的命令行版）：默认「以终稿为准」，线上用例换成文件里的并整份标为人工定稿。

用法（任何能访问服务的机器，只依赖标准库）：
    python scripts/upload_final.py --base https://tiangong.example.com --task cc20fddc02ce --user admin --file docs/终稿.xlsx [--file 第二个.xlsx]
密码从环境变量 TIANGONG_PASSWORD 读，没有则交互输入；绑定两步验证的账号加 --otp。--diff-only 只算差异留痕、不覆盖线上用例。
"""

from __future__ import annotations

import argparse
import getpass
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def request(base: str, path: str, token: str | None = None, body: dict | None = None,
            data: bytes | None = None, content_type: str | None = None) -> dict:
    payload = data if data is not None else (json.dumps(body, ensure_ascii=False).encode() if body is not None else None)
    req = urllib.request.Request(base.rstrip("/") + path, data=payload, method="POST" if payload is not None else "GET")
    req.add_header("Content-Type", content_type or "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except ValueError:
            pass
        raise SystemExit(f"{path} → {e.code}：{detail}")


def multipart(fields: dict[str, str], files: list[Path]) -> tuple[bytes, str]:
    boundary = "----tiangong" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for p in files:
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{p.name}\"\r\nContent-Type: {ctype}\r\n\r\n".encode()
        out += p.read_bytes() + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--file", action="append", required=True, help="终稿文件，可多次指定")
    ap.add_argument("--otp", default=None)
    ap.add_argument("--diff-only", action="store_true", help="只算差异留痕，不覆盖线上用例")
    args = ap.parse_args()
    paths = [Path(f) for f in args.file]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise SystemExit("文件不存在：" + "、".join(missing))

    password = os.environ.get("TIANGONG_PASSWORD") or getpass.getpass(f"{args.user} 的密码：")
    login = request(args.base, "/api/v1/auth/login", body={"username": args.user, "password": password, "otp": args.otp})
    if login.get("otp_required"):
        raise SystemExit("该账号绑定了两步验证，请加 --otp 动态码")
    token = login["token"]
    task = request(args.base, f"/api/v1/tasks/{args.task}", token)
    total = len((task.get("result") or {}).get("cases") or [])
    print(f"任务 {args.task}「{task.get('title') or ''}」线上 {total} 条用例；上传 {', '.join(p.name for p in paths)}"
          + ("（只算差异）" if args.diff_only else "（以终稿为准：覆盖线上用例并标为人工定稿）"))
    data, ctype = multipart({"apply": "false" if args.diff_only else "true"}, paths)
    r = request(args.base, f"/api/v1/tasks/{args.task}/final", token, data=data, content_type=ctype)
    s = r["stats"]
    print(f"差异：文件 {s['final']} 条 vs 线上 {s['generated']} 条 → 新增 {s['added']} / 删除 {s['deleted']} / 修改 {s['modified']} / 未改动 {s['unchanged']}，完全采纳率 {s['adoption_rate'] * 100:.1f}%")
    if r.get("applied"):
        a = r["apply_stats"]
        print(f"已按终稿覆盖：线上现在 {a['total']} 条用例，全部为人工定稿（改 {a['modified']} / 增 {a['added']} / 删 {a['deleted']}，删的在回收站）")
    request(args.base, "/api/v1/auth/logout", token, body={})


if __name__ == "__main__":
    sys.exit(main())
