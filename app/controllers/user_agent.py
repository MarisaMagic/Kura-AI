from typing import Optional

from tortoise.expressions import Q

from app.core.crud import CRUDBase
from app.models.user_agent import UserAgent
from app.schemas.user_agent import UserAgentCreate, UserAgentUpdate


class UserAgentController(CRUDBase[UserAgent, UserAgentCreate, UserAgentUpdate]):
    def __init__(self):
        super().__init__(model=UserAgent)

    async def get_owned(self, agent_id: int, user_id: int) -> Optional[UserAgent]:
        return await self.model.filter(id=agent_id, user_id=user_id).first()


user_agent_controller = UserAgentController()
