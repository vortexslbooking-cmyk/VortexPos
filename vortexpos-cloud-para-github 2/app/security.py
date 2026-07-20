"""
Seguridad: hash de PIN/contraseña (PBKDF2, biblioteca estándar) y tokens JWT.
No se guardan PINs ni contraseñas en claro.
"""
import os
import hmac
import hashlib
import base64
import time
from typing import Optional, Dict, Any

import jwt  # PyJWT

JWT_SECRET = os.environ.get("JWT_SECRET", "cambia-esta-clave-en-produccion")
JWT_ALG = "HS256"
TOKEN_TTL = int(os.environ.get("TOKEN_TTL_SECONDS", str(60 * 60 * 12)))  # 12 h

_PBKDF_ROUNDS = 120_000


def hash_secret(secret: str) -> str:
    """Devuelve 'salt$hash' en base64 (PBKDF2-HMAC-SHA256)."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, _PBKDF_ROUNDS)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_secret(secret: str, stored: str) -> bool:
    try:
        salt_b64, hash_b64 = stored.split("$", 1)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, _PBKDF_ROUNDS)
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def make_token(payload: Dict[str, Any]) -> str:
    data = dict(payload)
    data["exp"] = int(time.time()) + TOKEN_TTL
    data["iat"] = int(time.time())
    return jwt.encode(data, JWT_SECRET, algorithm=JWT_ALG)


def read_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except Exception:
        return None
