"""绑定 kb_scope 与智能体 LLM 配置的多模态搜索知识库工具。"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from app.chat.tools import (
    _set_last_rag_context,
    emit_rag_step,
    log_kb_tool_return_to_terminal,
)


def make_multimodal_search_tool(kb_scope: str, llm_config: dict[str, Any]) -> StructuredTool:
    def _search_multimodal_kb(query: str, search_mode: str = "all") -> str:
        """
        多模态搜索知识库
        
        Args:
            query: 搜索查询（文本）或base64编码的图片
            search_mode: 搜索模式
                - "all": 搜索所有类型（文本+图片）
                - "text": 仅搜索文本
                - "image": 仅搜索图片
        """
        from app.chat.tools import try_acquire_knowledge_tool_slot
        from app.kb.cross_modal_search import get_cross_modal_search_service

        if not try_acquire_knowledge_tool_slot():
            limit_msg = (
                "TOOL_CALL_LIMIT_REACHED: search_multimodal_kb has already been called once in this turn. "
                "Use existing retrieval result and provide final answer directly."
            )
            log_kb_tool_return_to_terminal(limit_msg)
            return limit_msg

        try:
            search_service = get_cross_modal_search_service()
            
            # 判断查询类型（文本还是图片）
            if query.startswith("data:image") or query.startswith("data:application"):
                # 图片查询
                import base64
                # 去掉 data URL 前缀
                if "," in query:
                    query = query.split(",", 1)[1]
                
                emit_rag_step("🖼️", "正在以图搜图/以图搜文...", f"模式: {search_mode}")
                result = search_service.search_by_image_base64(query, kb_scope, top_k=5, search_type=search_mode)
            else:
                # 文本查询
                include_images = search_mode in ["all", "image"]
                include_text = search_mode in ["all", "text"]
                
                emit_rag_step("🔍", "正在以文搜图/以文搜文...", f"模式: {search_mode}")
                result = search_service.search_by_text(query, kb_scope, top_k=5, include_images=include_images, include_text=include_text)
                
        except Exception as e:
            emit_rag_step("⚠️", "多模态知识库检索失败", str(e)[:200])
            _set_last_rag_context(
                {
                    "rag_trace": {
                        "tool_used": True,
                        "tool_name": "search_multimodal_kb",
                        "query": query,
                        "error": str(e),
                    }
                }
            )
            err_msg = f"多模态知识库检索出错：{e}"
            log_kb_tool_return_to_terminal(err_msg)
            return err_msg

        docs = result.get("docs", []) if isinstance(result, dict) else []
        meta = result.get("meta", {}) if isinstance(result, dict) else {}
        
        if meta:
            _set_last_rag_context({"rag_trace": meta})

        if not docs:
            empty_msg = "No relevant content found in the multimodal knowledge base."
            log_kb_tool_return_to_terminal(empty_msg)
            return empty_msg

        # 格式化检索结果
        formatted = []
        for i, doc in enumerate(docs, 1):
            content_type = doc.get("content_type", "text")
            filename = doc.get("filename", "Unknown")
            page = doc.get("page_number", "N/A")
            
            if content_type == "image":
                # 图片结果
                image_text = doc.get("text", "")
                image_metadata = doc.get("image_metadata", {})
                
                formatted_text = f"[{i}] 📷 图片：{filename} (第{page}页)\n"
                if (image_text or "").strip():
                    formatted_text += f"   文本：{image_text}\n"
                
                # 添加图片URL（如果有）
                if image_metadata:
                    from app.settings import settings
                    image_url = f"{settings.USER_AGENT_KB_IMAGES_URL_PREFIX}/{image_metadata.get('stored_relpath', '')}"
                    formatted_text += f"   图片链接：{image_url}\n"
                
                score = doc.get("score", 0.0)
                formatted_text += f"   相似度：{score:.4f}"
                
                formatted.append(formatted_text)
            else:
                # 文本结果
                text = doc.get("text", "")
                score = doc.get("score", 0.0)
                
                formatted_text = f"[{i}] 📄 {filename} (第{page}页)\n"
                formatted_text += f"   内容：{text}\n"
                formatted_text += f"   相似度：{score:.4f}"
                
                formatted.append(formatted_text)

        # 添加统计信息
        meta_info = []
        if "image_count" in meta:
            meta_info.append(f"找到 {meta['image_count']} 张图片")
        if "text_count" in meta:
            meta_info.append(f"找到 {meta['text_count']} 个文本片段")
        
        out = "📚 多模态检索结果：\n"
        if meta_info:
            out += f"   {'; '.join(meta_info)}\n\n"
        out += "\n\n".join(formatted)
        
        log_kb_tool_return_to_terminal(out)
        return out

    return StructuredTool.from_function(
        name="search_multimodal_kb",
        description=(
            "在本智能体「多模态知识库」中检索与查询相关的内容，支持："
            "1. 以文搜图：输入文本描述，检索相关图片；"
            "2. 以文搜文：输入文本，检索相关文本内容；"
            "3. 以图搜图：输入图片（base64编码），检索相似图片；"
            "4. 以图搜文：输入图片（base64编码），检索相关文本内容。"
            "参数说明："
            "- query: 查询内容（文本或base64编码的图片）；"
            "- search_mode: 搜索模式（all/image/text，默认all）。"
            "适用于：查询可能依赖已入库的图文资料、产品图片、图表数据等。"
            "调用约束：同一用户提问轮次内最多成功检索一次；得到工具返回后应直接整合为最终回答，勿重复检索。"
            "若返回无相关内容，应如实说明。"
        ),
        func=_search_multimodal_kb,
    )
