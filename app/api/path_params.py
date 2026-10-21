"""路径参数里的编码斜杠。

项目名允许含「/」（如「连赢活动10/20」），前端 encodeURIComponent 后是 %2F；但 uvicorn / httpx 在进路由之前
就把 %2F 解成「/」，`/api/v1/projects/{name}/contents` 收到的是 `/api/v1/projects/连赢活动10/20/contents`，
`{name}` 只匹配一段，整条路由匹不上，所有带项目名的接口一律 404。

两步补回：
1. ASGI 中间件按 raw_path 重算 scope["path"]：其余字符照常解码，只有 %2F 原样保留；
2. 把 Starlette 默认的 str 转换器换成这里的版本，参数值里的 %2F 还原成「/」，处理函数拿到的仍是原名。

名字里真有「%」时前端编成 %25（「x%2Fy」→ x%252Fy）：中间件重算路径时把解码出来的「%」再编回 %25，转换器统一做
一次完整 unquote，保留的 %2F 与名字里的字面 %2F 不混。必须在定义任何路由之前导入（转换器在路由编译时被查表取用），
因此由 app/api/routes.py 在文件头导入。
"""

from __future__ import annotations

import re
from urllib.parse import unquote, unquote_to_bytes

from starlette.convertors import StringConvertor, register_url_convertor

_ENCODED_SLASH = re.compile(rb"%2[fF]")
_NEEDS_REWRITE = re.compile(rb"%2[fF]|%25")
_KEEP = "%2F"


class SlashSafeStringConvertor(StringConvertor):
    """默认 `{name}`：匹配规则不变（不跨「/」），只是把保留下来的 %2F / 再编码的 %25 还原。

    中间件没改写的请求路径里不含「%」（服务器已解码；裸「%」只在畸形请求里出现），unquote 等于原样返回。"""

    def convert(self, value: str) -> str:
        return unquote(value)

    def to_string(self, value: str) -> str:
        value = str(value)
        assert value, "Must not be empty"
        return value.replace("/", _KEEP)


register_url_convertor("str", SlashSafeStringConvertor())


def path_keeping_encoded_slash(raw_path: bytes) -> str | None:
    """raw_path 含 %2F 或 %25 时返回「其余解码、%2F 保留、字面 % 编回 %25」的路径；否则返回 None（沿用服务器已解码的 path）。"""
    if not _NEEDS_REWRITE.search(raw_path):
        return None
    parts = [unquote_to_bytes(p).decode("utf-8", "replace").replace("%", "%25") for p in _ENCODED_SLASH.split(raw_path)]
    return _KEEP.join(parts)


class KeepEncodedSlashMiddleware:
    """纯 ASGI 中间件：只在 raw_path 含 %2F / %25 时改写 scope["path"]，其余请求零开销。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = path_keeping_encoded_slash(scope.get("raw_path") or b"")
            if path is not None:
                root = scope.get("root_path") or ""
                if root and scope.get("path", "").startswith(root) and not path.startswith(root):
                    path = root + path
                scope = dict(scope, path=path)
        await self.app(scope, receive, send)
