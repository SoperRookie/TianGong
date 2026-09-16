"""登录认证与用户管理（账号体系 v1）。

- 用户存储：入库（kv_docs，用户名 + PBKDF2 口令哈希 + 角色），旧 auth.json 首启自动迁移，
  无任何用户时自动创建管理员；
- 会话：登录签发随机 Bearer Token，固定有效期，入库可跨重启；
- 角色：系统级 admin（平台管理）与 member（平台使用）；项目级角色见 app.permissions；
- 用户资料（完整需求 3.1）：姓名/邮箱/手机/头像/状态（正常/禁用）/最后登录时间与 IP，
  禁用即会话失效、拒绝登录。
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

ROLES = ("admin", "member")
USER_STATUSES = ("active", "disabled")
PROFILE_FIELDS = ("name", "email", "phone", "avatar")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# 用户名：字母/数字/下划线/中文/点/@/连字符，1~64 位（不含引号与尖括号，避免进入内联属性时被利用）
_USERNAME_RE = re.compile(r"^[\w.@-]{1,64}$")
_DUMMY_SALT = "0" * 32

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


def _token_key(token: str) -> str:
    """会话令牌入库只存 sha256（DB 只读权限泄露不等于会话劫持）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthStore:
    def __init__(self, storage_path: Path, session_ttl_hours: int = 72,
                 max_failures: int = 5, lockout_minutes: int = 15):
        from app.db import DocStore

        self._path = storage_path  # 旧文件：仅用于首启迁移
        self._doc = DocStore("auth")
        self._ttl = timedelta(hours=session_ttl_hours)
        self._users: dict[str, dict] = {}
        self._sessions: dict[str, dict] = {}
        self._settings: dict = {"totp_enabled": True}
        # 登录防爆破：(用户名, IP) -> {count, locked_until}（进程内即可，配合单实例锁）
        self._failures: dict[tuple[str, str], dict] = {}
        self._max_failures = max_failures
        self._lockout = timedelta(minutes=lockout_minutes)
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
        self._sessions = {}
        for t, s in raw.get("sessions", {}).items():
            if s.get("expires_at", "") <= now:
                continue
            # 迁移：历史明文令牌改为哈希键（43 位 urlsafe → 64 位 hex）
            self._sessions[t if len(t) == 64 else _token_key(t)] = s

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
        """首启引导：无任何用户时创建管理员。

        未显式配置口令时生成随机口令只在日志打印一次，并强制首次登录改密。
        """
        if self._users:
            return
        generated = not password
        if generated:
            password = secrets.token_urlsafe(12)
        self.add_user(username, password, role="admin")
        if generated:
            self._users[username]["must_change_password"] = True
            self._persist()
            logger.warning("已创建初始管理员 {}，随机口令：{}（仅打印一次，首次登录须改密）", username, password)
        else:
            logger.info("已创建初始管理员账号 {}（请尽快登录并修改密码）", username)

    @staticmethod
    def _clean_profile(**fields) -> dict:
        """资料字段清洗：None 表示不修改；邮箱做格式校验；头像限长（内联 data URL 或地址）。"""
        out = {}
        for key, value in fields.items():
            if value is None:
                continue
            value = (value or "").strip()
            if key == "email" and value and not _EMAIL_RE.match(value):
                raise AuthError(f"邮箱格式不正确: {value}")
            if key == "phone" and value and not re.fullmatch(r"[+\d][\d\- ]{4,19}", value):
                raise AuthError(f"手机号格式不正确: {value}")
            if key == "avatar" and len(value) > 200_000:
                raise AuthError("头像过大（请使用 150KB 以内的图片）")
            if key in ("name",) and len(value) > 64:
                raise AuthError("姓名过长")
            out[key] = value
        return out

    def add_user(
        self, username: str, password: str, role: str = "member",
        name: str = "", email: str = "", phone: str = "",
    ) -> dict:
        username = username.strip()
        if not username:
            raise AuthError("用户名不能为空")
        if not _USERNAME_RE.match(username):
            raise AuthError("用户名只能含字母、数字、中文、下划线、点、@ 和连字符，且不超过 64 位")
        if username in self._users:
            raise AuthError(f"用户已存在: {username}")
        if role not in ROLES:
            raise AuthError(f"未知角色: {role}（可用 {'/'.join(ROLES)}）")
        if len(password) < 6:
            raise AuthError("密码长度至少 6 位")
        salt = secrets.token_hex(16)
        now = _now().isoformat(timespec="seconds")
        self._users[username] = {
            "username": username,
            "salt": salt,
            "password_hash": _hash_password(password, salt),
            "role": role,
            "status": "active",
            "created_at": now,
            "updated_at": now,
            **self._clean_profile(name=name, email=email, phone=phone),
        }
        self._persist()
        return self.public_user(username)

    def delete_user(self, username: str, operator: str) -> None:
        if username not in self._users:
            raise AuthError(f"用户不存在: {username}")
        if username == operator:
            raise AuthError("不能删除当前登录账号")
        if self._users[username]["role"] == "admin" and self._active_admins(exclude=username) == 0:
            raise AuthError("不能删除最后一个可用管理员")
        self._users.pop(username)
        self._sessions = {t: s for t, s in self._sessions.items() if s["username"] != username}
        self._persist()

    def admin_update(
        self,
        username: str,
        role: str | None = None,
        new_password: str | None = None,
        reset_totp: bool = False,
        status: str | None = None,
        operator: str | None = None,
        **profile,
    ) -> dict:
        """管理员管理用户：改角色 / 重置密码 / 重置两步验证 / 启用禁用 / 编辑资料。"""
        user = self._users.get(username)
        if user is None:
            raise AuthError(f"用户不存在: {username}")
        if status is not None and status != user.get("status", "active"):
            if status not in USER_STATUSES:
                raise AuthError(f"未知状态: {status}（可用 {'/'.join(USER_STATUSES)}）")
            if status == "disabled":
                if username == operator:
                    raise AuthError("不能禁用当前登录账号")
                if user["role"] == "admin" and self._active_admins(exclude=username) == 0:
                    raise AuthError("不能禁用最后一个可用管理员")
                # 禁用即会话失效（3.1）
                self._sessions = {t: s for t, s in self._sessions.items()
                                  if s["username"] != username}
            user["status"] = status
        user.update(self._clean_profile(**profile))
        if reset_totp:
            for key in ("totp_secret", "totp_enabled", "totp_last_counter", "totp_pending"):
                user.pop(key, None)
        if role and role != user["role"]:
            if role not in ROLES:
                raise AuthError(f"未知角色: {role}（可用 {'/'.join(ROLES)}）")
            if user["role"] == "admin" and self._active_admins(exclude=username) == 0:
                raise AuthError("不能降级最后一个可用管理员")
            user["role"] = role
        if new_password:
            if len(new_password) < 6:
                raise AuthError("密码长度至少 6 位")
            salt = secrets.token_hex(16)
            user["salt"], user["password_hash"] = salt, _hash_password(new_password, salt)
            # 重置密码后强制该用户重新登录
            self._sessions = {t: s for t, s in self._sessions.items() if s["username"] != username}
        user["updated_at"] = _now().isoformat(timespec="seconds")
        self._persist()
        return self.public_user(username)

    def update_profile(self, username: str, **profile) -> dict:
        """用户自助维护资料（姓名/邮箱/手机/头像）。"""
        user = self._users.get(username)
        if user is None:
            raise AuthError(f"用户不存在: {username}")
        user.update(self._clean_profile(**profile))
        user["updated_at"] = _now().isoformat(timespec="seconds")
        self._persist()
        return self.public_user(username)

    def exists(self, username: str) -> bool:
        return username in self._users

    def _active_admins(self, exclude: str | None = None) -> int:
        return sum(1 for u in self._users.values()
                   if u["role"] == "admin" and u.get("status", "active") == "active" and u["username"] != exclude)

    def list_users(self) -> list[dict]:
        return sorted((self.public_user(u) for u in self._users), key=lambda x: x["created_at"])

    def public_user(self, username: str) -> dict:
        u = self._users[username]
        return {
            "username": u["username"], "role": u["role"], "created_at": u["created_at"],
            "totp_enabled": bool(u.get("totp_enabled")),
            "name": u.get("name", ""), "email": u.get("email", ""), "phone": u.get("phone", ""),
            "avatar": u.get("avatar", ""), "status": u.get("status", "active"),
            "updated_at": u.get("updated_at", u["created_at"]),
            "last_login_at": u.get("last_login_at"), "last_login_ip": u.get("last_login_ip"),
            "must_change_password": bool(u.get("must_change_password")),
        }

    # ---- 登录会话 ----

    def login(
        self, username: str, password: str, otp: str | None = None, ip: str | None = None,
    ) -> tuple[str, dict]:
        from app.auth.totp import verify_totp

        username = username.strip()
        key = (username, ip or "")
        fail = self._failures.get(key)
        if fail and fail.get("locked_until") and fail["locked_until"] > _now():
            remain = int((fail["locked_until"] - _now()).total_seconds() // 60) + 1
            raise AuthError(f"登录失败次数过多，账号已锁定，请 {remain} 分钟后再试")
        user = self._users.get(username)
        # 用户不存在时也做一次等价哈希，避免响应时间侧信道枚举用户名
        ok = user is not None and _hash_password(password, user["salt"]) == user["password_hash"]
        if user is None:
            _hash_password(password, _DUMMY_SALT)
        if not ok:
            self._note_failure(key)
            raise AuthError("用户名或密码错误")
        if user.get("status", "active") != "active":
            raise AuthError("账号已被禁用，请联系管理员")
        if user.get("totp_enabled") and self.totp_policy():
            # 两步验证（TOTP，兼容 Google Authenticator / 海月盾等标准验证器）
            if not otp:
                raise OtpRequired()
            counter = verify_totp(user["totp_secret"], otp)
            if counter is None or counter <= int(user.get("totp_last_counter", -1)):
                self._note_failure(key)
                raise AuthError("动态验证码错误或已使用，请重新输入")
            user["totp_last_counter"] = counter  # 防重放：同一动态码只允许使用一次
        self._failures.pop(key, None)
        token = secrets.token_urlsafe(32)
        self._sessions[_token_key(token)] = {
            "username": user["username"],
            "expires_at": (_now() + self._ttl).isoformat(),
        }
        user["last_login_at"] = _now().isoformat(timespec="seconds")
        user["last_login_ip"] = ip or ""
        self._persist()
        return token, self.public_user(user["username"])

    def _note_failure(self, key: tuple[str, str]) -> None:
        fail = self._failures.setdefault(key, {"count": 0, "locked_until": None})
        fail["count"] += 1
        if fail["count"] >= self._max_failures:
            fail["locked_until"] = _now() + self._lockout
            fail["count"] = 0
            logger.warning("登录失败达到上限，锁定 {} 分钟：用户 {} IP {}", int(self._lockout.total_seconds() // 60), *key)
        # 防止字典无限增长
        if len(self._failures) > 10000:
            cutoff = _now()
            self._failures = {k: v for k, v in self._failures.items()
                              if v.get("locked_until") and v["locked_until"] > cutoff}

    def verify(self, token: str | None) -> dict | None:
        if not token:
            return None
        key = _token_key(token)
        session = self._sessions.get(key)
        if session is None:
            return None
        if session["expires_at"] <= _now().isoformat():
            self._sessions.pop(key, None)
            self._persist()
            return None
        user = self._users.get(session["username"])
        if user is None or user.get("status", "active") != "active":
            return None
        return self.public_user(user["username"])

    def logout(self, token: str | None) -> None:
        if token and self._sessions.pop(_token_key(token), None) is not None:
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
        if not self.totp_policy():
            raise AuthError("两步验证功能已被管理员关闭")
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
        counter = verify_totp(user["totp_secret"], code)
        if counter is None or counter <= int(user.get("totp_last_counter", -1)):
            raise AuthError("动态验证码错误或已使用")
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
        user.pop("must_change_password", None)
        # 改密后仅保留当前会话之外的失效：简单起见全部注销，需重新登录
        self._sessions = {t: s for t, s in self._sessions.items() if s["username"] != username}
        self._persist()
