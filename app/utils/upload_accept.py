"""文档上传受理辅助：大小/魔数校验 + 准入控制 + 流式暂存 + 任务创建。

供知识库与实验数据集上传接口共用，保证两条链路的受理语义一致：
- 队列模式（KB_UPLOAD_MODE=queue）：文件流式落对象存储 pending 区（不整份入内存），
  写任务 meta 后入队，立即返回 task_id；超限返回 429 由前端稍后重试。
- 内联模式：读入内存后交 API 进程线程池（本地开发）。
"""

from __future__ import annotations

import asyncio
import mimetypes
import os

from fastapi import UploadFile

from app.core import object_storage as obs
from app.kb import kb_job
from app.log import logger
from app.settings import settings
from app.utils.upload_sniff import assert_upload_magic


def _upload_size(file: UploadFile) -> int:
    """上传文件字节数；优先用 Starlette 解析好的 size，兜底 seek/tell。"""
    size = getattr(file, "size", None)
    if size is not None:
        try:
            return int(size)
        except (TypeError, ValueError):
            pass
    try:
        f = file.file
        pos = f.tell()
        f.seek(0, os.SEEK_END)
        total = f.tell()
        f.seek(pos)
        return int(total)
    except Exception:  # noqa: BLE001
        return 0


async def prepare_document_upload(
    *,
    file: UploadFile,
    display_filename: str,
    user_id: int,
    agent_id: int,
    kb_scope: str,
) -> tuple[str | None, tuple[int, str] | None]:
    """
    受理单个文档上传。
    :return: (task_id, None) 成功；(None, (http_code, 错误文案)) 失败
    """
    max_bytes = max(1, int(settings.KB_UPLOAD_MAX_BYTES or 50 * 1024 * 1024))
    size = _upload_size(file)
    if size <= 0:
        return None, (400, "空文件")
    if size > max_bytes:
        return None, (400, f"文件超过大小上限 {max_bytes // (1024 * 1024)}MB，请拆分后重试")

    # 魔数校验只读头部；队列模式随后 seek 回起点流式上传
    head = await file.read(4096)
    try:
        assert_upload_magic(display_filename, head)
    except ValueError as e:
        return None, (400, str(e))

    if kb_job.upload_mode() == "queue":
        reason = await asyncio.to_thread(kb_job.admission_reason, user_id)
        if reason:
            return None, (429, reason)
        task_id = kb_job.new_task_id()
        source_key = kb_job.pending_source_key(task_id, display_filename)
        mime = mimetypes.guess_type(display_filename)[0] or "application/octet-stream"
        try:
            await file.seek(0)
            await asyncio.to_thread(obs.save_stream, source_key, file.file, size, mime)
        except Exception as e:  # noqa: BLE001
            logger.warning("上传暂存失败 filename={!r}: {}", display_filename, e)
            return None, (503, "文件暂存失败，请稍后重试")
        created = await kb_job.create_kb_upload_job(
            user_id=user_id,
            agent_id=agent_id,
            kb_scope=kb_scope,
            display_filename=display_filename,
            source_key=source_key,
            size=size,
            task_id=task_id,
        )
        if not created:
            await asyncio.to_thread(obs.delete_key, source_key)
            return None, (503, "任务状态初始化失败（Redis 暂不可用），请稍后重试")
        return created, None

    # 内联模式（本地开发）：读入内存后交 API 线程池
    content = head + await file.read()
    created = await kb_job.create_kb_upload_job(
        user_id=user_id,
        agent_id=agent_id,
        kb_scope=kb_scope,
        display_filename=display_filename,
        content=content,
        size=size,
    )
    if not created:
        return None, (503, "任务状态初始化失败（Redis 暂不可用），请稍后重试")
    return created, None