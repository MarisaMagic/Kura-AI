import re
from datetime import datetime
from typing import List, Optional

from fastapi.exceptions import HTTPException

from app.core.crud import CRUDBase
from app.models.admin import Role, User
from app.schemas.login import CredentialsSchema, RegisterSchema
from app.schemas.users import UserCreate, UserUpdate
from app.settings import settings
from app.utils.password import get_password_hash, validate_password_strength, verify_password

from .role import role_controller


class UserController(CRUDBase[User, UserCreate, UserUpdate]):
    def __init__(self):
        super().__init__(model=User)

    async def get_by_email(self, email: str) -> Optional[User]:
        return await self.model.filter(email=email.strip().lower()).first()

    async def get_by_username(self, username: str) -> Optional[User]:
        return await self.model.filter(username=username).first()

    async def create_user(self, obj_in: UserCreate) -> User:
        obj_in.password = get_password_hash(password=obj_in.password)
        obj = await self.create(obj_in)
        return obj

    async def update_last_login(self, id: int) -> None:
        user = await self.model.get(id=id)
        user.last_login = datetime.now()
        await user.save()

    async def authenticate(self, credentials: CredentialsSchema) -> Optional["User"]:
        account = credentials.username.strip()
        if "@" in account:
            user = await self.get_by_email(account.lower())
        else:
            user = await self.get_by_username(account)
        if not user:
            raise HTTPException(status_code=400, detail="无效的账号或密码")
        verified = verify_password(credentials.password, user.password)
        if not verified:
            raise HTTPException(status_code=400, detail="无效的账号或密码")
        if not user.is_active:
            raise HTTPException(status_code=400, detail="用户已被禁用")
        return user

    async def _unique_username(self, preferred: str) -> str:
        base = re.sub(r"[^a-zA-Z0-9_]", "_", preferred).strip("_")[:20]
        if len(base) < 3:
            base = "user"
        if not await self.get_by_username(base):
            return base
        for i in range(1, 10000):
            suffix = f"_{i}"
            candidate = f"{base[: 20 - len(suffix)]}{suffix}"
            if not await self.get_by_username(candidate):
                return candidate
        raise HTTPException(status_code=400, detail="无法生成唯一用户名，请手动指定用户名")

    async def register_user(self, body: RegisterSchema) -> User:
        email = body.email.strip().lower()
        if email == settings.INITIAL_ADMIN_EMAIL.strip().lower():
            raise HTTPException(status_code=400, detail="该邮箱不可用于注册")
        if await self.get_by_email(email):
            raise HTTPException(status_code=400, detail="该邮箱已被注册")

        username = body.username
        if username:
            if username.lower() == settings.INITIAL_ADMIN_USERNAME.lower():
                raise HTTPException(status_code=400, detail="该用户名不可使用")
            if await self.get_by_username(username):
                raise HTTPException(status_code=400, detail="用户名已被占用")
        else:
            local = email.split("@", 1)[0]
            username = await self._unique_username(local)
            if username.lower() == settings.INITIAL_ADMIN_USERNAME.lower():
                username = await self._unique_username(f"{local}_user")

        try:
            validate_password_strength(body.password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        user = await self.create_user(
            UserCreate(
                email=email,
                username=username,
                password=body.password,
                is_active=True,
                is_superuser=False,
            )
        )
        role = await Role.filter(name="普通用户").first()
        if role:
            await self.update_roles(user, [role.id])
        return user

    async def update_roles(self, user: User, role_ids: List[int]) -> None:
        await user.roles.clear()
        for role_id in role_ids:
            role_obj = await role_controller.get(id=role_id)
            await user.roles.add(role_obj)

    async def reset_password(self, user_id: int, new_password: str) -> None:
        user_obj = await self.get(id=user_id)
        if user_obj.is_superuser:
            raise HTTPException(status_code=403, detail="不允许重置超级管理员密码")
        try:
            user_obj.password = get_password_hash(new_password)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        await user_obj.save()


user_controller = UserController()
