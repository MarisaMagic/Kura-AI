import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator


class CredentialsSchema(BaseModel):
    username: str = Field(..., description="用户名或邮箱", example="admin")
    password: str = Field(..., description="密码", min_length=1, max_length=128)


class RegisterSchema(BaseModel):
    email: EmailStr = Field(..., description="邮箱")
    password: str = Field(..., description="密码", min_length=8, max_length=128)
    username: Optional[str] = Field(None, description="用户名（可选，默认由邮箱前缀生成）", max_length=20)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if not re.fullmatch(r"[a-zA-Z0-9_]{3,20}", v):
            raise ValueError("用户名须为 3–20 位字母、数字或下划线")
        return v


class JWTOut(BaseModel):
    access_token: str
    username: str


class JWTPayload(BaseModel):
    user_id: int
    username: str
    is_superuser: bool
    exp: datetime
