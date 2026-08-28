"""登录认证与用户管理（账号体系 v1）。

- 用户存储：data/auth.json（用户名 + PBKDF2 口令哈希 + 角色），首启自动创建管理员；
- 会话：登录签发随机 Bearer Token，固定有效期，落盘可跨重启；
- 角色：admin（用户管理 / 模型配置）与 member（平台使用）。
多用户项目权限隔离（F-8-8）后续在此基础上扩展。
"""

from __future__ import annotations

import hashlib
import json
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


class AuthStore:
    def __init__(self, storage_path: Path, session_ttl_hours: int = 72):
        self._path = storage_path
        self._ttl = timedelta(hours=session_ttl_hours)
        self._users: dict[str, dict] = {}
        self._sessions: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        self._users = raw.get("users", {})
        now = _now().isoformat()
        self._sessions = {
            t: s for t, s in raw.get("sessions", {}).items() if s.get("expires_at", "") > now
        }

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"users": self._users, "sessions": self._sessions}, ensure_ascii=False),
            encoding="utf-8",
        )

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

    def list_users(self) -> list[dict]:
        return sorted((self.public_user(u) for u in self._users), key=lambda x: x["created_at"])

    def public_user(self, username: str) -> dict:
        u = self._users[username]
        return {"username": u["username"], "role": u["role"], "created_at": u["created_at"]}

    # ---- 登录会话 ----

    def login(self, username: str, password: str) -> tuple[str, dict]:
        user = self._users.get(username.strip())
        if user is None or _hash_password(password, user["salt"]) != user["password_hash"]:
            raise AuthError("用户名或密码错误")
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
