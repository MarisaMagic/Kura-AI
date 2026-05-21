import secrets
import string

from passlib import pwd
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

_WEAK_PASSWORDS = frozenset(
    {
        "123456",
        "12345678",
        "password",
        "admin123",
        "qwerty123",
        "11111111",
    }
)


def validate_password_strength(password: str, *, min_length: int = 8) -> None:
    """校验密码强度；不通过则抛出 ValueError。"""
    if len(password) < min_length:
        raise ValueError(f"密码至少 {min_length} 位")
    if len(password) > 128:
        raise ValueError("密码过长")
    if password.lower() in _WEAK_PASSWORDS:
        raise ValueError("密码过于简单，请更换")
    if password.isdigit() or password.isalpha():
        raise ValueError("密码需同时包含字母与数字")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    validate_password_strength(password)
    return pwd_context.hash(password)


def generate_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    for _ in range(32):
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        try:
            validate_password_strength(candidate)
            return candidate
        except ValueError:
            continue
    return pwd.genword() + "1aA"
