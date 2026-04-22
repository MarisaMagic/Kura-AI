"""绑定 kb_scope 与智能体 LLM 配置的 search_knowledge_base 工具（多模态：文本 + 图片元数据来自 PostgreSQL）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool

from app.chat.tools import (
    _set_last_rag_context,
    emit_rag_step,
    log_kb_tool_return_to_terminal,
)
from app.settings import settings


def _kb_image_public_url(stored_relpath: str) -> str:
    """
    将 mg_kb_images.stored_relpath 转为浏览器可访问路径或完整 URL。
    配置了 PUBLIC_API_BASE 时返回绝对 URL，否则返回以 / 开头的 API 相对路径。
    """
    relpath = (stored_relpath or "").strip().replace("\\", "/")
    if not relpath:
        return ""
    prefix = (settings.USER_AGENT_KB_IMAGES_URL_PREFIX or "").strip().rstrip("/")
    path_part = f"{prefix}/{relpath.lstrip('/')}"
    base = (getattr(settings, "PUBLIC_API_BASE", None) or "").strip().rstrip("/")
    if base:
        return f"{base}{path_part}"
    return path_part


def _kb_image_absolute_fs_path(stored_relpath: str) -> Path:
    return Path(settings.USER_AGENT_KB_IMAGES_ROOT) / (stored_relpath or "").strip().replace("\\", "/")


def make_search_knowledge_tool(kb_scope: str, llm_config: dict[str, Any]) -> StructuredTool:
    def _search_knowledge_base(query: str) -> str:
        from app.chat.tools import try_acquire_knowledge_tool_slot

        if not try_acquire_knowledge_tool_slot():
            limit_msg = (
                "TOOL_CALL_LIMIT_REACHED: search_knowledge_base has already been called once in this turn. "
                "Use existing retrieval result and provide final final answer directly."
            )
            log_kb_tool_return_to_terminal(limit_msg)
            return limit_msg

        try:
            from app.kb.rag_pipeline import run_rag_graph

            rag_result = run_rag_graph(
                question=query.strip(),
                kb_scope=kb_scope,
                llm_config=llm_config,
            )
        except Exception as e:
            emit_rag_step("⚠️", "知识库检索失败", str(e)[:200])
            _set_last_rag_context(
                {
                    "rag_trace": {
                        "tool_used": True,
                        "tool_name": "search_knowledge_base",
                        "query": query,
                        "error": str(e),
                    }
                }
            )
            err_msg = f"知识库检索出错：{e}"
            log_kb_tool_return_to_terminal(err_msg)
            return err_msg

        docs = rag_result.get("docs", []) if isinstance(rag_result, dict) else []
        rag_trace = rag_result.get("rag_trace", {}) if isinstance(rag_result, dict) else {}

        image_references: list[dict[str, Any]] = []

        if not docs:
            empty_msg = "No relevant documents found in knowledge base."
            log_kb_tool_return_to_terminal(empty_msg)
            _set_last_rag_context({"rag_trace": rag_trace, "image_references": []})
            return empty_msg

        formatted: list[str] = []
        image_count = 0

        for i, result in enumerate(docs, 1):
            source = result.get("filename", "Unknown")
            page = result.get("page_number", "N/A")
            content_type = result.get("content_type", "text")
            score = result.get("score", 0.0)

            if content_type == "image":
                image_metadata = result.get("image_metadata", {})
                if not image_metadata:
                    continue

                image_count += 1
                chunk_id = result.get("chunk_id", "")
                width = image_metadata.get("width", 0)
                height = image_metadata.get("height", 0)
                img_format = image_metadata.get("format", "png")
                stored_relpath = (image_metadata.get("stored_relpath") or "").strip()
                img_id = image_metadata.get("id", "")

                img_info = f"[图片 {width}x{height}, {img_format}]"
                chunk_text = f"[{i}] {source} (Page {page}) - {img_info}\nchunk_id: {chunk_id}\nScore: {score:.4f}"
                formatted.append(chunk_text)

                if not stored_relpath:
                    formatted.append("（PostgreSQL 中无 stored_relpath，无法生成图片链接）")
                    continue

                public_url = _kb_image_public_url(stored_relpath)
                on_disk = _kb_image_absolute_fs_path(stored_relpath).is_file()

                formatted.append(f"PostgreSQL stored_relpath（知识库图片表中的相对存储路径）: {stored_relpath}")
                if img_id:
                    formatted.append(f"PostgreSQL mg_kb_images.id: {img_id}")
                formatted.append(f"本地文件已落盘: {'是' if on_disk else '否'}")
                formatted.append(
                    f"图片公网访问 URL（回答中展示图片时必须原样使用该字符串，Markdown 示例: ![]({public_url}) ）: {public_url}"
                )
                if not (getattr(settings, "PUBLIC_API_BASE", None) or "").strip():
                    formatted.append(
                        "提示：未配置 PUBLIC_API_BASE 时为相对路径；请在 .env 设置 PUBLIC_API_BASE=http://主机:端口 "
                        "以便模型获得完整 http(s) 链接（前端 Markdown 渲染同样需要可访问的绝对 URL）。"
                    )

                image_references.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": public_url,
                            "chunk_id": chunk_id,
                            "stored_relpath": stored_relpath,
                            "kb_image_id": img_id,
                            "page_number": page,
                            "filename": source,
                            "width": width,
                            "height": height,
                        },
                    }
                )
            else:
                text = result.get("text", "")
                formatted.append(f"[{i}] {source} (Page {page})\n{text}\nScore: {score:.4f}")

        if image_count > 0:
            formatted.insert(0, f"检索结果：{len(docs)} 个文档（包含 {image_count} 张图片）\n")
        else:
            formatted.insert(0, f"检索结果：{len(docs)} 个文档\n")

        out = "Retrieved Chunks:\n" + "\n\n---\n\n".join(formatted)
        log_kb_tool_return_to_terminal(out)

        _set_last_rag_context(
            {
                "rag_trace": rag_trace,
                "image_references": image_references,
            }
        )

        return out

    return StructuredTool.from_function(
        name="search_knowledge_base",
        description=(
            "在本智能体「知识库」中检索与用户问题相关的文档片段（多模态：文本 + 图片）。"
            "图片的存储路径以 PostgreSQL（mg_kb_images.stored_relpath）为准；工具返回中会给出「图片公网访问 URL」。"
            "回答用户时若需配图，必须使用工具返回的完整 http(s) URL 写入 Markdown 图片语法，与原文字符完全一致；"
            "不得使用 image://、file://、序号或自拟路径。"
            "调用约束：同一用户提问轮次内最多成功检索一次；得到工具返回后应直接整合为最终回答，勿重复检索。"
        ),
        func=_search_knowledge_base,
    )
