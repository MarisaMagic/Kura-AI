"""
跨模态检索服务，支持以文搜图、以图搜图、以图搜文。
使用多模态嵌入模型实现跨模态语义检索（向量相似度）。
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from app.kb.image_store import get_image_store
from app.kb.multimodal_embedding import get_multimodal_embedding_service
from app.kb.milvus_client import MilvusManager, kb_filter_expr, milvus_escape
from app.settings import settings


class CrossModalSearchService:
    """跨模态检索服务"""

    def __init__(self) -> None:
        """初始化 CrossModalSearchService"""
        self.embedding_service = get_multimodal_embedding_service()
        self.milvus_manager = MilvusManager()
        self.image_store = get_image_store()

    def search_by_text(
        self,
        query_text: str,
        kb_scope: str,
        top_k: int = 5,
        include_images: bool = True,
        include_text: bool = True,
    ) -> Dict[str, Any]:
        """
        以文搜图/以文搜文
        :param query_text: 查询文本
        :param kb_scope: 知识库范围
        :param top_k: 返回结果数量
        :param include_images: 是否包含图片
        :param include_text: 是否包含文本
        :return: 检索结果
        """
        if not include_images and not include_text:
            return {"docs": [], "meta": {}}
        
        # 构建过滤表达式
        filter_parts = []
        if include_images and include_text:
            # 搜索所有类型
            filter_expr = kb_filter_expr(kb_scope)
        elif include_images:
            filter_expr = f'{kb_filter_expr(kb_scope)} && content_type == "image"'
        else:
            filter_expr = f'{kb_filter_expr(kb_scope)} && content_type == "text"'
        
        try:
            # 获取查询文本的密集向量和稀疏向量
            dense_embeddings = self.embedding_service.get_text_embeddings([query_text])
            dense_embedding = dense_embeddings[0]
            sparse_embedding = self.embedding_service.get_sparse_embedding(query_text)
            
            # 混合检索
            results = self.milvus_manager.hybrid_retrieve(
                dense_embedding=dense_embedding,
                sparse_embedding=sparse_embedding,
                top_k=top_k,
                filter_expr=filter_expr,
            )
            
            # 获取相关图片的完整信息
            if include_images:
                image_chunks = [r for r in results if r.get("content_type") == "image"]
                if image_chunks:
                    chunk_ids = [c.get("chunk_id") for c in image_chunks]
                    images = self.image_store.get_images_by_chunk_ids(chunk_ids)
                    image_map = {img.chunk_id: img for img in images}
                    
                    for chunk in results:
                        if chunk.get("content_type") == "image":
                            chunk_id = chunk.get("chunk_id")
                            if chunk_id in image_map:
                                img = image_map[chunk_id]
                                chunk["image_metadata"] = {
                                    "id": img.id,
                                    "stored_relpath": img.stored_relpath,
                                    "display_filename": img.display_filename,
                                    "file_size": img.file_size,
                                    "width": img.width,
                                    "height": img.height,
                                    "format": img.format,
                                    "position_x": img.position_x,
                                    "position_y": img.position_y,
                                    "position_width": img.position_width,
                                    "position_height": img.position_height,
                                    "related_text_ids": img.related_text_ids,
                                }
            
            meta = {
                "search_type": "text_to_all",
                "include_images": include_images,
                "include_text": include_text,
                "total_results": len(results),
                "image_count": len([r for r in results if r.get("content_type") == "image"]),
                "text_count": len([r for r in results if r.get("content_type") == "text"]),
            }
            
            return {"docs": results, "meta": meta}
            
        except Exception as e:
            logger.error(f"Failed to search by text: {e}")
            return {
                "docs": [],
                "meta": {
                    "error": str(e),
                    "search_type": "text_to_all",
                },
            }

    def search_by_image(
        self,
        image_path: str,
        kb_scope: str,
        top_k: int = 5,
        search_type: str = "all",  # "all", "image", "text"
    ) -> Dict[str, Any]:
        """
        以图搜图/以图搜文
        :param image_path: 查询图片路径
        :param kb_scope: 知识库范围
        :param top_k: 返回结果数量
        :param search_type: 搜索类型
        :return: 检索结果
        """
        if not Path(image_path).exists():
            return {
                "docs": [],
                "meta": {
                    "error": f"Image not found: {image_path}",
                    "search_type": "image_to_all",
                },
            }
        
        # 构建过滤表达式
        if search_type == "all":
            filter_expr = kb_filter_expr(kb_scope)
        elif search_type == "image":
            filter_expr = f'{kb_filter_expr(kb_scope)} && content_type == "image"'
        elif search_type == "text":
            filter_expr = f'{kb_filter_expr(kb_scope)} && content_type == "text"'
        else:
            filter_expr = kb_filter_expr(kb_scope)
        
        try:
            # 获取查询图片的密集向量
            dense_embeddings = self.embedding_service.get_image_embeddings([image_path])
            dense_embedding = dense_embeddings[0]
            # 图片的稀疏向量使用空字典
            sparse_embedding = {}
            
            # 检索（只使用密集向量，因为没有图片的稀疏向量）
            results = self.milvus_manager.dense_retrieve(
                dense_embedding=dense_embedding,
                top_k=top_k,
                filter_expr=filter_expr,
            )
            
            # 获取相关图片的完整信息
            if search_type in ["all", "image"]:
                image_chunks = [r for r in results if r.get("content_type") == "image"]
                if image_chunks:
                    chunk_ids = [c.get("chunk_id") for c in image_chunks]
                    images = self.image_store.get_images_by_chunk_ids(chunk_ids)
                    image_map = {img.chunk_id: img for img in images}
                    
                    for chunk in results:
                        if chunk.get("content_type") == "image":
                            chunk_id = chunk.get("chunk_id")
                            if chunk_id in image_map:
                                img = image_map[chunk_id]
                                chunk["image_metadata"] = {
                                    "id": img.id,
                                    "stored_relpath": img.stored_relpath,
                                    "display_filename": img.display_filename,
                                    "file_size": img.file_size,
                                    "width": img.width,
                                    "height": img.height,
                                    "format": img.format,
                                    "position_x": img.position_x,
                                    "position_y": img.position_y,
                                    "position_width": img.position_width,
                                    "position_height": img.position_height,
                                    "related_text_ids": img.related_text_ids,
                                }
            
            meta = {
                "search_type": "image_to_all",
                "target_type": search_type,
                "total_results": len(results),
                "image_count": len([r for r in results if r.get("content_type") == "image"]),
                "text_count": len([r for r in results if r.get("content_type") == "text"]),
            }
            
            return {"docs": results, "meta": meta}
            
        except Exception as e:
            logger.error(f"Failed to search by image: {e}")
            return {
                "docs": [],
                "meta": {
                    "error": str(e),
                    "search_type": "image_to_all",
                },
            }

    def search_by_image_base64(
        self,
        image_base64: str,
        kb_scope: str,
        top_k: int = 5,
        search_type: str = "all",
    ) -> Dict[str, Any]:
        """
        以图搜图/以图搜文（使用base64编码的图片）
        :param image_base64: base64编码的图片
        :param kb_scope: 知识库范围
        :param top_k: 返回结果数量
        :param search_type: 搜索类型
        :return: 检索结果
        """
        import tempfile
        
        try:
            # 解码base64图片并保存到临时文件
            image_data = base64.b64decode(image_base64)
            
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_file:
                tmp_file.write(image_data)
                tmp_path = tmp_file.name
            
            # 使用临时文件进行检索
            result = self.search_by_image(tmp_path, kb_scope, top_k, search_type)
            
            # 删除临时文件
            Path(tmp_path).unlink(missing_ok=True)
            
            return result
            
        except Exception as e:
            logger.error(f"Failed to search by image base64: {e}")
            return {
                "docs": [],
                "meta": {
                    "error": str(e),
                    "search_type": "image_to_all",
                },
            }

    def get_related_images_for_texts(
        self,
        text_chunk_ids: List[str],
    ) -> List[Dict[str, Any]]:
        """
        根据文本块ID列表获取关联的图片信息
        :param text_chunk_ids: 文本块ID列表
        :return: 图片信息列表
        """
        if not text_chunk_ids:
            return []
        
        images = []
        for text_chunk_id in text_chunk_ids:
            related_images = self.image_store.get_related_images_for_text(text_chunk_id)
            for img in related_images:
                images.append({
                    "id": img.id,
                    "chunk_id": img.chunk_id,
                    "stored_relpath": img.stored_relpath,
                    "display_filename": img.display_filename,
                    "page_number": img.page_number,
                    "width": img.width,
                    "height": img.height,
                    "format": img.format,
                    "position_x": img.position_x,
                    "position_y": img.position_y,
                    "related_text_ids": img.related_text_ids,
                })
        
        return images

    def format_search_results(
        self,
        results: List[Dict[str, Any]],
        include_image_urls: bool = True,
    ) -> str:
        """
        格式化检索结果为文本
        :param results: 检索结果列表
        :param include_image_urls: 是否包含图片URL
        :return: 格式化的文本
        """
        if not results:
            return "未找到相关结果。"
        
        formatted_chunks = []
        
        for i, result in enumerate(results, 1):
            content_type = result.get("content_type", "text")
            filename = result.get("filename", "Unknown")
            page = result.get("page_number", "N/A")
            score = result.get("score", 0.0)
            
            if content_type == "image":
                # 图片块
                image_metadata = result.get("image_metadata", {})
                
                chunk_text = f"[{i}] 图片：{filename} (第{page}页)\n"
                
                # 添加图片尺寸信息
                if image_metadata:
                    width = image_metadata.get("width", 0)
                    height = image_metadata.get("height", 0)
                    chunk_text += f"图片尺寸：{width}x{height}\n"
                
                if include_image_urls and image_metadata:
                    image_url = f"{settings.USER_AGENT_KB_IMAGES_URL_PREFIX}/{image_metadata.get('stored_relpath', '')}"
                    chunk_text += f"图片链接：{image_url}\n"
                
                chunk_text += f"相似度：{score:.4f}"
                formatted_chunks.append(chunk_text)
            else:
                # 文本块
                text = result.get("text", "")
                chunk_text = f"[{i}] {filename} (第{page}页)\n{text}\n相似度：{score:.4f}"
                formatted_chunks.append(chunk_text)
        
        return "\n\n---\n\n".join(formatted_chunks)


# 全局跨模态检索服务实例
_cross_modal_search_service = None


def get_cross_modal_search_service() -> CrossModalSearchService:
    """
    获取全局跨模态检索服务实例
    :return: CrossModalSearchService
    """
    global _cross_modal_search_service
    if _cross_modal_search_service is None:
        _cross_modal_search_service = CrossModalSearchService()
    return _cross_modal_search_service
