"""安全工具：PBKDF2 密码哈希 + JWT（HS256）签发/校验。

密码哈希用标准库 hashlib/hmac 自实现 PBKDF2-SHA256（零额外依赖）；
JWT 用 PyJWT（HS256，需要 requirements.txt 中的 PyJWT 依赖）。
"""

import base64
import hashlib
import hmac
import os
import time

import jwt  # PyJWT

from backend.core.config import JWT_SECRET, JWT_ALGORITHM, JWT_EXPIRE_MINUTES

# ---- PBKDF2 密码哈希 ----

_PBKDF2_ITERATIONS = 200_000
_ALGO_TAG = "pbkdf2_sha256"


def hash_password(password: str) -> str:
    """生成存储格式：pbkdf2_sha256$<iterations>$<salt_b64>$<dk_b64>"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return (
        f"{_ALGO_TAG}${_PBKDF2_ITERATIONS}"
        f"${base64.b64encode(salt).decode()}"
        f"${base64.b64encode(dk).decode()}"
    )


def verify_password(password: str, hashed: str) -> bool:
    """校验密码与存储哈希是否匹配（恒定时间比较，防时序攻击）。"""
    try:
        algo, iterations, salt_b64, dk_b64 = hashed.split("$")
        if algo != _ALGO_TAG:
            return False
        salt = base64.b64decode(salt_b64)
        expect = base64.b64decode(dk_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    except Exception:
        return False
    return hmac.compare_digest(actual, expect)


# ---- JWT ----

def create_access_token(username: str, role: str) -> str:
    """签发 JWT：sub=用户名、role=角色、exp=过期时间。"""
    now = int(time.time())
    payload = {
        "sub": username,
        "role": role,
        "iat": now,
        "exp": now + JWT_EXPIRE_MINUTES * 60,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict | None:
    """校验并解析 JWT；无效/过期返回 None。"""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None