"""智能体 MCP 服务配置：增删改查、连接测试、预置商店。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from app.controllers.user_agent import user_agent_controller
from app.core.dependency import AuthControl
from app.mcp_client.presets import MCP_SERVER_PRESETS
from app.mcp_client.service import (
    decrypt_headers,
    encrypt_headers,
    test_mcp_server_connection,
)
from app.models import User
from app.models.user_agent_mcp import UserAgentMcpServer
from app.schemas.base import Fail, Success
from app.schemas.user_agent_mcp import (
    McpPresetItem,
    McpPresetListResponse,
    UserAgentMcpServerCreate,
    UserAgentMcpServerItem,
    UserAgentMcpServerListResponse,
    UserAgentMcpServerTestRequest,
    UserAgentMcpServerTestResponse,
    UserAgentMcpServerUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _to_item(row: UserAgentMcpServer) -> UserAgentMcpServerItem:
    headers = decrypt_headers(row.headers_ciphertext)
    return UserAgentMcpServerItem(
        id=row.id,
        name=row.name,
        description=row.description,
        transport=row.transport,
        url=row.url,
        enabled=bool(row.enabled),
        confirm_policy=(row.confirm_policy or "auto"),
        header_keys=sorted(headers.keys()),
        created_at=row.created_at.strftime("%Y-%m-%d %H:%M:%S") if row.created_at else "",
        updated_at=row.updated_at.strftime("%Y-%m-%d %H:%M:%S") if row.updated_at else "",
    )


async def _get_owned_server(agent_id: int, server_id: int, user_id: int):
    """校验智能体归属并取出该智能体下的指定 MCP 配置。"""
    ua = await user_agent_controller.get_owned(agent_id, user_id)
    if not ua:
        return None, None
    row = await UserAgentMcpServer.get_or_none(id=server_id, agent_id=agent_id)
    return ua, row


@router.get("/mcp/servers", summary="智能体 MCP 服务列表", tags=["智能体模块"])
async def list_mcp_servers(
    agent_id: int = Query(..., description="智能体 ID"),
    current_user: User = Depends(AuthControl.is_authed),
):
    ua = await user_agent_controller.get_owned(agent_id, current_user.id)
    if not ua:
        return Fail(code=404, msg="智能体不存在或无权限访问")
    rows = await UserAgentMcpServer.filter(agent_id=agent_id).order_by("-updated_at").all()
    return Success(
        data=UserAgentMcpServerListResponse(servers=[_to_item(r) for r in rows]).model_dump()
    )


@router.post("/mcp/servers/create", summary="新增智能体 MCP 服务", tags=["智能体模块"])
async def create_mcp_server(
    request: UserAgentMcpServerCreate,
    agent_id: int = Query(..., description="智能体 ID"),
    current_user: User = Depends(AuthControl.is_authed),
):
    ua = await user_agent_controller.get_owned(agent_id, current_user.id)
    if not ua:
        return Fail(code=404, msg="智能体不存在或无权限访问")
    row = await UserAgentMcpServer.create(
        agent_id=agent_id,
        user_id=current_user.id,
        name=request.name.strip(),
        description=(request.description or "").strip() or None,
        transport=request.transport,
        url=request.url,
        headers_ciphertext=encrypt_headers(request.headers),
        enabled=request.enabled,
        confirm_policy=request.confirm_policy,
    )
    return Success(data=_to_item(row).model_dump())


@router.post("/mcp/servers/update", summary="更新智能体 MCP 服务", tags=["智能体模块"])
async def update_mcp_server(
    request: UserAgentMcpServerUpdate,
    agent_id: int = Query(..., description="智能体 ID"),
    server_id: int = Query(..., description="MCP 服务 ID"),
    current_user: User = Depends(AuthControl.is_authed),
):
    ua, row = await _get_owned_server(agent_id, server_id, current_user.id)
    if not ua:
        return Fail(code=404, msg="智能体不存在或无权限访问")
    if not row:
        return Fail(code=404, msg="MCP 服务配置不存在")

    if request.name is not None:
        row.name = request.name.strip()
    if request.description is not None:
        row.description = request.description.strip() or None
    if request.transport is not None:
        row.transport = request.transport
    if request.url is not None:
        row.url = request.url
    if request.headers is not None:
        # {} 表示清空；非空覆盖
        row.headers_ciphertext = encrypt_headers(request.headers)
    if request.enabled is not None:
        row.enabled = request.enabled
    if request.confirm_policy is not None:
        row.confirm_policy = request.confirm_policy
    await row.save()
    return Success(data=_to_item(row).model_dump())


@router.delete("/mcp/servers/delete", summary="删除智能体 MCP 服务", tags=["智能体模块"])
async def delete_mcp_server(
    agent_id: int = Query(..., description="智能体 ID"),
    server_id: int = Query(..., description="MCP 服务 ID"),
    current_user: User = Depends(AuthControl.is_authed),
):
    ua, row = await _get_owned_server(agent_id, server_id, current_user.id)
    if not ua:
        return Fail(code=404, msg="智能体不存在或无权限访问")
    if not row:
        return Fail(code=404, msg="MCP 服务配置不存在")
    await row.delete()
    return Success(data={"ok": True})


@router.post("/mcp/servers/test", summary="测试 MCP 服务连接（list_tools）", tags=["智能体模块"])
async def test_mcp_server(
    request: UserAgentMcpServerTestRequest,
    agent_id: int = Query(..., description="智能体 ID"),
    current_user: User = Depends(AuthControl.is_authed),
):
    """
    测试连接：创建/编辑弹窗与列表页共用。
    headers 为空且携带 server_id 时，使用库中已保存的请求头测试。
    """
    ua = await user_agent_controller.get_owned(agent_id, current_user.id)
    if not ua:
        return Fail(code=404, msg="智能体不存在或无权限访问")

    headers = request.headers
    if headers is None and request.server_id is not None:
        saved = await UserAgentMcpServer.get_or_none(id=request.server_id, agent_id=agent_id)
        if not saved:
            return Fail(code=404, msg="MCP 服务配置不存在")
        headers = decrypt_headers(saved.headers_ciphertext)

    try:
        result = await test_mcp_server_connection(request.transport, request.url, headers)
        return Success(data=UserAgentMcpServerTestResponse(**result).model_dump())
    except Exception as e:
        logger.warning("MCP server connection test failed: %s", str(e)[:1000])
        return Success(
            data=UserAgentMcpServerTestResponse(ok=False, error="MCP 服务连接失败").model_dump()
        )


@router.get("/mcp/presets", summary="MCP 预置商店列表", tags=["智能体模块"])
async def list_mcp_presets(current_user: User = Depends(AuthControl.is_authed)):
    return Success(
        data=McpPresetListResponse(
            presets=[McpPresetItem(**p) for p in MCP_SERVER_PRESETS]
        ).model_dump()
    )
