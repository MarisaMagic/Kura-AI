"""
图片存储服务，用于管理知识库图片的存储、检索和删除。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from sqlalchemy import select
from loguru import logger

from app.chat.database import SessionLocal
from app.chat.db_models import KbImage
from app.settings import settings


class ImageStore:
    """图片存储服务"""

    def __init__(self) -> None:
        """初始化 ImageStore"""
        pass

    def save_image(
        self,
        kb_scope: str,
        user_id: int,
        agent_id: int,
        filename: str,
        image_path: str,
        page_number: int = 0,
        chunk_id: Optional[str] = None,
        parent_chunk_id: Optional[str] = None,
        root_chunk_id: Optional[str] = None,
        position_x: int = 0,
        position_y: int = 0,
        position_width: int = 0,
        position_height: int = 0,
        image_width: int = 0,
        image_height: int = 0,
        image_format: str = "png",
        related_text_ids: Optional[List[str]] = None,
    ) -> KbImage:
        """
        保存图片元数据到数据库
        :param kb_scope: 知识库范围
        :param user_id: 用户ID
        :param agent_id: 智能体ID
        :param filename: 文件名
        :param image_path: 图片存储路径
        :param page_number: 页码
        :param chunk_id: 关联的向量块ID
        :param parent_chunk_id: 关联的父块ID
        :param root_chunk_id: 关联的根块ID
        :param position_x: 图片在页面中的X坐标
        :param position_y: 图片在页面中的Y坐标
        :param position_width: 图片在页面中的宽度
        :param position_height: 图片在页面中的高度
        :param image_width: 图片实际宽度
        :param image_height: 图片实际高度
        :param image_format: 图片格式
        :param related_text_ids: 关联的文本块ID列表
        :return: KbImage
        """
        db = SessionLocal()
        try:
            # 获取图片文件信息
            path_obj = Path(image_path)
            file_size = path_obj.stat().st_size if path_obj.exists() else 0
            
            # 计算相对路径
            images_root = Path(settings.USER_AGENT_KB_IMAGES_ROOT)
            try:
                stored_relpath = str(path_obj.relative_to(images_root))
            except ValueError:
                stored_relpath = image_path
            
            # 创建图片记录
            image_record = KbImage(
                id=uuid.uuid4().hex,
                kb_scope=kb_scope,
                filename=filename,
                display_filename=Path(image_path).name,
                stored_relpath=stored_relpath,
                file_size=file_size,
                mime_type=self._get_mime_type(image_path),
                width=image_width,
                height=image_height,
                format=image_format,
                caption="",
                embedding_model=settings.EMBEDDING_MODEL,
                source_document=filename,
                page_number=page_number,
                position_x=position_x,
                position_y=position_y,
                position_width=position_width,
                position_height=position_height,
                chunk_id=chunk_id,
                parent_chunk_id=parent_chunk_id,
                root_chunk_id=root_chunk_id,
                related_text_ids=related_text_ids or [],
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            
            db.add(image_record)
            db.commit()
            db.refresh(image_record)
            
            logger.info(f"Saved image metadata: {image_record.id}")
            return image_record
            
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to save image metadata: {e}")
            raise
        finally:
            db.close()

    def save_images_batch(
        self,
        image_chunks: List[dict],
    ) -> int:
        """
        批量保存图片元数据
        :param image_chunks: 图片块列表
        :return: 保存的数量
        """
        saved_count = 0
        for chunk in image_chunks:
            if chunk.get("content_type") != "image":
                continue
            
            # 获取图片元数据
            image_metadata = chunk.get("image_metadata", {})
            
            try:
                self.save_image(
                    kb_scope=chunk.get("kb_scope", ""),
                    user_id=chunk.get("user_id", 0),
                    agent_id=chunk.get("agent_id", 0),
                    filename=chunk.get("filename", ""),
                    image_path=chunk.get("image_path", ""),
                    page_number=chunk.get("page_number", 0),
                    chunk_id=chunk.get("chunk_id", ""),
                    parent_chunk_id=chunk.get("parent_chunk_id", ""),
                    root_chunk_id=chunk.get("root_chunk_id", ""),
                    position_x=chunk.get("image_position_x", 0),
                    position_y=chunk.get("image_position_y", 0),
                    position_width=chunk.get("image_width", 0),
                    position_height=chunk.get("image_height", 0),
                    image_width=image_metadata.get("width", 0),
                    image_height=image_metadata.get("height", 0),
                    image_format=image_metadata.get("format", "png"),
                    related_text_ids=chunk.get("related_text_ids", []),
                )
                saved_count += 1
            except Exception as e:
                logger.error(f"Failed to save image chunk: {e}")
        
        return saved_count

    def get_images_by_kb_scope(self, kb_scope: str) -> List[KbImage]:
        """
        根据知识库范围获取图片列表
        :param kb_scope: 知识库范围
        :return: 图片列表
        """
        db = SessionLocal()
        try:
            stmt = select(KbImage).where(KbImage.kb_scope == kb_scope)
            result = db.execute(stmt).scalars().all()
            return list(result)
        finally:
            db.close()

    def get_images_by_chunk_ids(self, chunk_ids: List[str]) -> List[KbImage]:
        """
        根据chunk_ids获取图片列表
        :param chunk_ids: chunk_id列表
        :return: 图片列表
        """
        if not chunk_ids:
            return []
        
        db = SessionLocal()
        try:
            stmt = select(KbImage).where(KbImage.chunk_id.in_(chunk_ids))
            result = db.execute(stmt).scalars().all()
            return list(result)
        finally:
            db.close()

    def get_images_by_page(self, kb_scope: str, page_number: int) -> List[KbImage]:
        """
        根据知识库范围和页码获取图片列表
        :param kb_scope: 知识库范围
        :param page_number: 页码
        :return: 图片列表
        """
        db = SessionLocal()
        try:
            stmt = select(KbImage).where(
                KbImage.kb_scope == kb_scope,
                KbImage.page_number == page_number
            ).order_by(KbImage.position_y)
            result = db.execute(stmt).scalars().all()
            return list(result)
        finally:
            db.close()

    def get_related_images_for_text(self, text_chunk_id: str) -> List[KbImage]:
        """
        根据文本块ID获取关联的图片列表
        :param text_chunk_id: 文本块ID
        :return: 图片列表
        """
        db = SessionLocal()
        try:
            # 查询 related_text_ids 包含该 text_chunk_id 的图片
            # 注意：PostgreSQL JSON 数组查询；方言/列类型不兼容时降级为空列表，避免整次检索失败
            stmt = select(KbImage).where(
                KbImage.related_text_ids.contains([text_chunk_id])
            )
            result = db.execute(stmt).scalars().all()
            return list(result)
        except Exception as e:
            logger.warning("get_related_images_for_text 查询失败 chunk_id={!r}: {}", text_chunk_id, e)
            return []
        finally:
            db.close()

    def delete_images_by_kb_scope(self, kb_scope: str) -> int:
        """
        根据知识库范围删除图片
        :param kb_scope: 知识库范围
        :return: 删除的数量
        """
        db = SessionLocal()
        try:
            # 先获取要删除的图片列表
            stmt = select(KbImage).where(KbImage.kb_scope == kb_scope)
            images = db.execute(stmt).scalars().all()
            
            # 删除图片文件
            for image in images:
                try:
                    image_path = Path(settings.USER_AGENT_KB_IMAGES_ROOT) / image.stored_relpath
                    if image_path.exists():
                        image_path.unlink()
                except Exception as e:
                    logger.warning(f"Failed to delete image file {image.stored_relpath}: {e}")
            
            # 删除数据库记录
            stmt = select(KbImage).where(KbImage.kb_scope == kb_scope)
            images = db.execute(stmt).scalars().all()
            count = len(images)
            
            for image in images:
                db.delete(image)
            
            db.commit()
            logger.info(f"Deleted {count} images for kb_scope: {kb_scope}")
            return count
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to delete images: {e}")
            return 0
        finally:
            db.close()

    def delete_images_by_document(self, kb_scope: str, filename: str) -> int:
        """
        根据知识库范围和文件名删除图片
        :param kb_scope: 知识库范围
        :param filename: 文件名
        :return: 删除的数量
        """
        db = SessionLocal()
        try:
            # 先获取要删除的图片列表
            stmt = select(KbImage).where(
                KbImage.kb_scope == kb_scope,
                KbImage.source_document == filename
            )
            images = db.execute(stmt).scalars().all()
            
            # 删除图片文件
            for image in images:
                try:
                    image_path = Path(settings.USER_AGENT_KB_IMAGES_ROOT) / image.stored_relpath
                    if image_path.exists():
                        image_path.unlink()
                except Exception as e:
                    logger.warning(f"Failed to delete image file {image.stored_relpath}: {e}")
            
            # 删除数据库记录
            count = len(images)
            for image in images:
                db.delete(image)
            
            db.commit()
            logger.info(f"Deleted {count} images for {filename} in {kb_scope}")
            return count
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to delete images: {e}")
            return 0
        finally:
            db.close()

    def _get_mime_type(self, image_path: str) -> str:
        """
        获取图片的MIME类型
        :param image_path: 图片路径
        :return: MIME类型
        """
        ext = Path(image_path).suffix.lower()
        mime_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".bmp": "image/bmp",
            ".webp": "image/webp",
        }
        return mime_types.get(ext, "image/jpeg")


# 全局图片存储服务实例
_image_store = None


def get_image_store() -> ImageStore:
    """
    获取全局图片存储服务实例
    :return: ImageStore
    """
    global _image_store
    if _image_store is None:
        _image_store = ImageStore()
    return _image_store
