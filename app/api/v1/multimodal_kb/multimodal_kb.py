"""多模态知识库 API 路由"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from loguru import logger

from app.settings import settings

router = APIRouter()


@router.get("/media/user_agent_images/{file_path:path}", tags=["基础模块"])
async def get_kb_image(file_path: str) -> FileResponse:
    """
    获取知识库图片
    :param file_path: 图片相对路径
    :return: 图片文件
    """
    try:
        # 构建完整路径
        image_path = Path(settings.USER_AGENT_KB_IMAGES_ROOT) / file_path
        
        # 检查文件是否存在
        if not image_path.exists():
            logger.warning(f"Image not found: {image_path}")
            raise HTTPException(status_code=404, detail="Image not found")
        
        # 检查是否为文件
        if not image_path.is_file():
            logger.warning(f"Path is not a file: {image_path}")
            raise HTTPException(status_code=404, detail="Image not found")
        
        # 获取 MIME 类型
        mime_type = _get_mime_type(image_path.suffix)
        
        # 返回文件
        return FileResponse(
            path=str(image_path),
            media_type=mime_type,
            filename=image_path.name,
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get image: {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}") from e


def _get_mime_type(extension: str) -> str:
    """
    根据文件扩展名获取 MIME 类型
    :param extension: 文件扩展名
    :return: MIME 类型
    """
    mime_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
    }
    return mime_types.get(extension.lower(), "image/jpeg")
