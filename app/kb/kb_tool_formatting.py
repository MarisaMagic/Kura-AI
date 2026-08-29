"""知识库工具返回的格式化：文本以文/以图检索共用。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.settings import settings
from app.utils.content_guard import guard_untrusted_content
from app.utils.signed_media import KIND_KB_IMAGE, sign_media_url


def _kb_image_public_url(stored_relpath: str) -> str:
    """
    获取图片的公网访问 URL
    :param stored_relpath: 知识库图片表中的相对存储路径
    :return: 公网访问 URL
    """
    relpath = (stored_relpath or "").strip().replace("\\", "/")
    if not relpath:
        return ""
    return sign_media_url(KIND_KB_IMAGE, relpath, absolute=True)


def _kb_image_absolute_fs_path(stored_relpath: str) -> Path:
    """
    获取图片的本地文件系统路径
    :param stored_relpath: 知识库图片表中的相对存储路径
    :return: 本地文件系统路径
    """
    return Path(settings.USER_AGENT_KB_IMAGES_ROOT) / (stored_relpath or "").strip().replace("\\", "/")


def format_knowledge_retrieval_tool_output(
    docs: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """
    将 RAG/检索产出的 docs 格式化为给模型的字符串与多模态 image_references 列表。
    返回 (工具输出正文, image_references, kb_sources)；
    kb_sources 的 index 与正文中 [i] 编号一致，供前端渲染「来源」列表。
    """
    image_references: list[dict[str, Any]] = []
    kb_sources: list[dict[str, Any]] = []
    if not docs:
        return "No relevant documents found in knowledge base.", [], []

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

            public_url = _kb_image_public_url(stored_relpath)
            kb_sources.append(
                {
                    "index": i,
                    "filename": source,
                    "page_number": page,
                    "chunk_id": chunk_id,
                    "score": score,
                    "content_type": "image",
                    "image_url": public_url,
                }
            )

            if not stored_relpath:
                formatted.append("（PostgreSQL 中无 stored_relpath，无法生成图片链接）")
                continue

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
            kb_sources.append(
                {
                    "index": i,
                    "filename": source,
                    "page_number": page,
                    "chunk_id": result.get("chunk_id", ""),
                    "score": score,
                    "content_type": "text",
                }
            )

    if image_count > 0:
        formatted.insert(0, f"检索结果：{len(docs)} 个文档（包含 {image_count} 张图片）\n")
    else:
        formatted.insert(0, f"检索结果：{len(docs)} 个文档\n")

    out = "Retrieved Chunks:\n" + "\n\n---\n\n".join(formatted)
    # 知识库原文属非可信外部内容：隔离包裹 + 长度上限，缓解间接提示注入
    out = guard_untrusted_content(out)
    return out, image_references, kb_sources
