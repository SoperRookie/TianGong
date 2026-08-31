"""登录认证与用户管理（账号体系 v1）。

- 用户存储：入库（kv_docs，用户名 + PBKDF2 口令哈希 + 角色），旧 auth.json 首启自动迁移，
  无任何用户时自动创建管理员；
- 会话：登录签发随机 Bearer Token，固定有效期，入库可跨重启；
- 角色：admin（用户管理 / 模型配置）与 member（平台使用）。
多用户项目权限隔离（F-8-8）后续在此基础上扩展。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

ROLES = ("admin", "member")

_PBKDF2_ITERATIONS = 120_000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS
    ).hex()


class AuthError(ValueError):
    pass


class OtpRequired(Exception):
    """口令校验通过但需补充动态验证码（登录二段式流程）。"""
    pass


class AuthStore:
    def __init__(self, storage_path: Path, session_ttl_hours: int = 72):
        from app.db import DocStore

        self._path = storage_path  # 旧文件：仅用于首启迁移
        self._doc = DocStore("auth")
        self._ttl = timedelta(hours=session_ttl_hours)
        self._users: dict[str, dict] = {}
        self._sessions: dict[str, dict] = {}
        self._settings: dict = {"totp_enabled": True}
        self._load()

    def _load(self) -> None:
        from app.db import load_with_migration

        raw = load_with_migration(
            self._doc, self._path,
            lambda data: {k: data.get(k, {}) for k in ("users", "sessions", "settings")},
        )
        self._users = raw.get("users", {})
        self._settings.update(raw.get("settings", {}))
        now = _now().isoformat()
        self._sessions = {
            t: s for t, s in raw.get("sessions", {}).items() if s.get("expires_at", "") > now
        }

    def _persist(self) -> None:
        self._doc.replace_all(
            {"users": self._users, "sessions": self._sessions, "settings": self._settings}
        )

    # ---- 安全设置（系统级开关）----

    def totp_policy(self) -> bool:
        """两步验证功能总开关：关闭时全平台登录不校验动态码、不可新绑定。"""
        return bool(self._settings.get("totp_enabled", True))

    def set_totp_policy(self, enabled: bool) -> None:
        self._settings["totp_enabled"] = bool(enabled)
        self._persist()

    # ---- 用户管理 ----

    def ensure_admin(self, username: str, password: str) -> None:
        """首启引导：无任何用户时创建管理员（口令经 TIANGONG_ADMIN_PASSWORD 配置）。"""
        if self._users:
            return
        self.add_user(username, password, role="admin")
        logger.info("已创建初始管理员账号 {}（请尽快登录并修改密码）", username)

    def add_user(self, username: str, password: str, role: str = "member") -> dict:
        username = username.strip()
        if not username:
            raise AuthError("用户名不能为空")
        if username in self._users:
            raise AuthError(f"用户已存在: {username}")
        if role not in ROLES:
            raise AuthError(f"未知角色: {role}（可用 {'/'.join(ROLES)}）")
        if len(password) < 6:
            raise AuthError("密码长度至少 6 位")
        salt = secrets.token_hex(16)
        self._users[username] = {
            "username": username,
            "salt": salt,
            "password_hash": _hash_password(password, salt),
            "role": role,
            "created_at": _now().isoformat(timespec="seconds"),
        }
        self._persist()
        return self.public_user(username)

    def delete_user(self, username: str, operator: str) -> None:
        if username not in self._users:
            raise AuthError(f"用户不存在: {username}")
        if username == operator:
            raise AuthError("不能删除当前登录账号")
        admins = [u for u in self._users.values() if u["role"] == "admin"]
        if self._users[username]["role"] == "admin" and len(admins) <= 1:
            raise AuthError("不能删除最后一个管理员")
        self._users.pop(username)
        self._sessions = {t: s for t, s in self._sessions.items() if s["username"] != username}
        self._persist()

    def admin_update(
        self,
        username: str,
        role: str | None = None,
        new_password: str | None = None,
        reset_totp: bool = False,
    ) -> dict:
        """管理员管理用户：修改角色 / 重置密码 / 重置两步验证（手机丢失等场景解绑）。"""
        user = self._users.get(username)
        if user is None:
            raise AuthError(f"用户不存在: {username}")
        if reset_totp:
            for key in ("totp_secret", "totp_enabled", "totp_last_counter", "totp_pending"):
                user.pop(key, None)
        if role and role != user["role"]:
            if role not in ROLES:
                raise AuthError(f"未知角色: {role}（可用 {'/'.join(ROLES)}）")
            admins = [u for u in self._users.values() if u["role"] == "admin"]
            if user["role"] == "admin" and len(admins) <= 1:
                raise AuthError("不能降级最后一个管理员")
            user["role"] = role
        if new_password:
            if len(new_password) < 6:
                raise AuthError("密码长度至少 6 位")
            salt = secrets.token_hex(16)
            user["salt"], user["password_hash"] = salt, _hash_password(new_password, salt)
            # 重置密码后强制该用户重新登录
            self._sessions = {t: s for t, s in self._sessions.items() if s["username"] != username}
        self._persist()
        return self.public_user(username)

    def list_users(self) -> list[dict]:
        return sorted((self.public_user(u) for u in self._users), key=lambda x: x["created_at"])

    def public_user(self, username: str) -> dict:
        u = self._users[username]
        return {"username": u["username"], "role": u["role"], "created_at": u["created_at"],
                "totp_enabled": bool(u.get("totp_enabled"))}

    # ---- 登录会话 ----

    def login(self, username: str, password: str, otp: str | None = None) -> tuple[str, dict]:
        from app.auth.totp import verify_totp

        user = self._users.get(username.strip())
        if user is None or _hash_password(password, user["salt"]) != user["password_hash"]:
            raise AuthError("用户名或密码错误")
        if user.get("totp_enabled") and self.totp_policy():
            # 两步验证（TOTP，兼容 Google Authenticator / 海月盾等标准验证器）
            if not otp:
                raise OtpRequired()
            counter = verify_totp(user["totp_secret"], otp)
            if counter is None or counter <= int(user.get("totp_last_counter", -1)):
                raise AuthError("动态验证码错误或已使用，请重新输入")
            user["totp_last_counter"] = counter  # 防重放：同一动态码只允许使用一次
        token = secrets.token_urlsafe(32)
        self._sessions[token] = {
            "username": user["username"],
            "expires_at": (_now() + self._ttl).isoformat(),
        }
        self._persist()
        return token, self.public_user(user["username"])

    def verify(self, token: str | None) -> dict | None:
        if not token:
            return None
        session = self._sessions.get(token)
        if session is None:
            return None
        if session["expires_at"] <= _now().isoformat():
            self._sessions.pop(token, None)
            self._persist()
            return None
        user = self._users.get(session["username"])
        return self.public_user(user["username"]) if user else None

    def logout(self, token: str | None) -> None:
        if token and self._sessions.pop(token, None) is not None:
            self._persist()

    # ---- 两步验证（TOTP）----

    def totp_setup(self, username: str) -> str:
        """开始绑定：生成待确认密钥（验证器扫码后须回填动态码确认才生效）。"""
        from app.auth.totp import generate_secret

        user = self._users.get(username)
        if user is None:
            raise AuthError(f"用户不存在: {username}")
        if not self.totp_policy():
            raise AuthError("两步验证功能已被管理员关闭")
        if user.get("totp_enabled"):
            raise AuthError("已绑定两步验证；如需换绑请先解绑或联系管理员重置")
        user["totp_pending"] = generate_secret()
        self._persist()
        return user["totp_pending"]

    def totp_enable(self, username: str, code: str) -> None:
        """确认绑定：验证器产出的动态码校验通过后正式启用。"""
        from app.auth.totp import verify_totp

        user = self._users.get(username)
        if user is None or not user.get("totp_pending"):
            raise AuthError("请先获取绑定二维码")
        counter = verify_totp(user["totp_pending"], code)
        if counter is None:
            raise AuthError("动态验证码错误，请确认验证器时间同步后重试")
        user["totp_secret"] = user.pop("totp_pending")
        user["totp_enabled"] = True
        user["totp_last_counter"] = counter
        self._persist()

    def totp_disable(self, username: str, password: str, code: str) -> None:
        """解绑：需同时校验密码与当前动态码。"""
        from app.auth.totp import verify_totp

        user = self._users.get(username)
        if user is None or not user.get("totp_enabled"):
            raise AuthError("未绑定两步验证")
        if _hash_password(password, user["salt"]) != user["password_hash"]:
            raise AuthError("密码错误")
        if verify_totp(user["totp_secret"], code) is None:
            raise AuthError("动态验证码错误")
        for key in ("totp_secret", "totp_enabled", "totp_last_counter", "totp_pending"):
            user.pop(key, None)
        self._persist()

    def change_password(self, username: str, old_password: str, new_password: str) -> None:
        user = self._users.get(username)
        if user is None or _hash_password(old_password, user["salt"]) != user["password_hash"]:
            raise AuthError("原密码错误")
        if len(new_password) < 6:
            raise AuthError("新密码长度至少 6 位")
        salt = secrets.token_hex(16)
        user["salt"], user["password_hash"] = salt, _hash_password(new_password, salt)
        # 改密后仅保留当前会话之外的失效：简单起见全部注销，需重新登录
        self._sessions = {t: s for t, s in self._sessions.items() if s["username"] != username}
        self._persist()
