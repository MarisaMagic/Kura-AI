"""用户智能体 API Key 的字段级加解密（Fernet）。"""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.settings.config import settings


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    if settings.API_KEY_ENCRYPTION_KEY:
        return Fernet(settings.API_KEY_ENCRYPTION_KEY.strip().encode())
    digest = hashlib.sha256((settings.SECRET_KEY + "\x1dMG-Agent:user-agent-api-key").encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_api_key(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_api_key(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()


def decrypt_api_key_safe(ciphertext: str | None) -> str | None:
    if not ciphertext:
        return None
    try:
        return decrypt_api_key(ciphertext)
    except InvalidToken:
        return None
