"""TOTP 动态口令（RFC 6238）：兼容 Google Authenticator / 海月盾等标准验证器。

标准参数：SHA1 / 30 秒周期 / 6 位数字；校验允许 ±1 个时间窗（时钟偏差容忍）。
纯标准库实现，无外部依赖。
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

PERIOD = 30
DIGITS = 6


def generate_secret() -> str:
    """生成 Base32 密钥（160bit，标准验证器均支持）。"""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


def totp_at(secret_b32: str, counter: int) -> str:
    key = base64.b32decode(secret_b32)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (int.from_bytes(digest[offset:offset + 4], "big") & 0x7FFFFFFF) % (10 ** DIGITS)
    return f"{code:0{DIGITS}d}"


def current_counter(at: float | None = None) -> int:
    return int((at if at is not None else time.time()) // PERIOD)


def totp_now(secret_b32: str, at: float | None = None) -> str:
    return totp_at(secret_b32, current_counter(at))


def verify_totp(secret_b32: str, code: str, at: float | None = None, window: int = 1) -> int | None:
    """校验动态码（±window 个时间窗），命中返回对应 counter（供防重放记录），失败返回 None。"""
    code = (code or "").strip()
    if not code.isdigit() or len(code) != DIGITS:
        return None
    now = current_counter(at)
    for counter in range(now - window, now + window + 1):
        if hmac.compare_digest(totp_at(secret_b32, counter), code):
            return counter
    return None


def otpauth_uri(secret_b32: str, username: str, issuer: str = "天工测试平台") -> str:
    """标准 otpauth URI：验证器 App 扫码或手输均可绑定。"""
    label = f"{quote(issuer)}:{quote(username)}"
    return f"otpauth://totp/{label}?secret={secret_b32}&issuer={quote(issuer)}&period={PERIOD}&digits={DIGITS}"


def qr_svg(text: str) -> str:
    """二维码 SVG（本地生成，不依赖外部服务）。"""
    import io

    import qrcode
    import qrcode.image.svg

    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=14)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode("utf-8")
