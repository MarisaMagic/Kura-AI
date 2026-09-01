"""对象存储封装（MinIO / S3 兼容）。

应用层全部用户文件（头像、会话附件、知识库文档与图片）统一存对象存储，
数据库仅保存对象 key（沿用原相对路径约定）。开发与生产均通过 S3_* 配置接入。
minio SDK 的 client 线程安全，本模块以惰性单例复用。
"""

from __future__ import annotations

import io
import os
import tempfile
import threading
from contextlib import contextmanager
from typing import Iterator, Optional, Tuple

from loguru import logger
from minio import Minio
from minio.deleteobjects import DeleteObject
from minio.error import S3Error

from app.settings.config import settings

_client: Optional[Minio] = None
_client_lock = threading.Lock()

_NOT_FOUND_CODES = {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}


def get_client() -> Minio:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = Minio(
                    settings.S3_ENDPOINT,
                    access_key=settings.S3_ACCESS_KEY,
                    secret_key=settings.S3_SECRET_KEY,
                    secure=settings.S3_SECURE,
                )
    return _client


def join_key(*parts: str) -> str:
    """以正斜杠拼接对象 key；忽略空段；剥离各段首尾斜杠（Windows 反斜杠一并归一）。"""
    cleaned = []
    for p in parts:
        s = str(p or "").replace("\\", "/").strip("/")
        if s:
            cleaned.append(s)
    return "/".join(cleaned)


def _validate_key(key: str) -> str:
    k = (key or "").replace("\\", "/").lstrip("/")
    if not k:
        raise ValueError("对象 key 为空")
    if any(seg == ".." for seg in k.split("/")):
        raise ValueError(f"非法对象 key（含 .. 段）：{key}")
    return k


class ObjectNotFoundError(FileNotFoundError):
    """对象不存在（与磁盘时代的 FileNotFoundError 语义对齐，便于调用方兼容）。"""

    def __init__(self, key: str):
        super().__init__(f"对象不存在: {key}")
        self.key = key


def ensure_bucket() -> None:
    """启动时确保 bucket 存在；连接失败会抛异常，由调用方决定是否阻止启动。"""
    client = get_client()
    if not client.bucket_exists(settings.S3_BUCKET):
        client.make_bucket(settings.S3_BUCKET)
        logger.info(f"对象存储 bucket 已创建: {settings.S3_BUCKET}")
    else:
        logger.info(f"对象存储已连接: {settings.S3_ENDPOINT}/{settings.S3_BUCKET}")


def save_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    key = _validate_key(key)
    get_client().put_object(
        settings.S3_BUCKET,
        key,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type,
    )
    return key


def save_file(key: str, file_path: str, content_type: str = "application/octet-stream") -> str:
    """从本地文件上传（避免大文件二次读入内存，供批量抽图上传等场景）。"""
    key = _validate_key(key)
    get_client().fput_object(settings.S3_BUCKET, key, file_path, content_type=content_type)
    return key


def read_bytes(key: str) -> bytes:
    key = _validate_key(key)
    resp = None
    try:
        resp = get_client().get_object(settings.S3_BUCKET, key)
        return resp.read()
    except S3Error as e:
        if e.code in _NOT_FOUND_CODES:
            raise ObjectNotFoundError(key) from e
        raise
    finally:
        if resp is not None:
            resp.close()
            resp.release_conn()


def stream_object(key: str, chunk_size: int = 64 * 1024) -> Tuple[Iterator[bytes], int, str]:
    """返回 (chunk 迭代器, content_length, content_type)；迭代器结束时关闭底层连接。"""
    key = _validate_key(key)
    try:
        stat = get_client().stat_object(settings.S3_BUCKET, key)
    except S3Error as e:
        if e.code in _NOT_FOUND_CODES:
            raise ObjectNotFoundError(key) from e
        raise
    resp = get_client().get_object(settings.S3_BUCKET, key)

    def _gen() -> Iterator[bytes]:
        try:
            for chunk in resp.stream(chunk_size):
                yield chunk
        finally:
            resp.close()
            resp.release_conn()

    return _gen(), int(stat.size), stat.content_type or "application/octet-stream"


def exists(key: str) -> bool:
    key = _validate_key(key)
    try:
        get_client().stat_object(settings.S3_BUCKET, key)
        return True
    except S3Error as e:
        if e.code in _NOT_FOUND_CODES:
            return False
        raise


def delete_key(key: str) -> None:
    """删除单个对象；对象不存在视为成功（幂等）。"""
    key = _validate_key(key)
    try:
        get_client().remove_object(settings.S3_BUCKET, key)
    except S3Error as e:
        if e.code not in _NOT_FOUND_CODES:
            raise


def delete_prefix(prefix: str) -> int:
    """删除前缀下全部对象，返回删除数量。空前缀直接拒绝，防误删整桶。"""
    prefix = (prefix or "").replace("\\", "/").strip("/")
    if not prefix:
        raise ValueError("拒绝空前缀删除")
    client = get_client()
    names = [
        o.object_name
        for o in client.list_objects(settings.S3_BUCKET, prefix=prefix + "/", recursive=True)
    ]
    if not names:
        return 0
    errors = list(client.remove_objects(settings.S3_BUCKET, (DeleteObject(n) for n in names)))
    if errors:
        detail = "; ".join(f"{e.object_name}: {e.message}" for e in errors[:5])
        raise RuntimeError(f"对象批量删除存在失败（{len(errors)}/{len(names)}）: {detail}")
    return len(names)


@contextmanager
def download_temp(key: str, suffix: str = "") -> Iterator[str]:
    """下载对象到本地临时文件，供只接受本地路径的解析库使用；退出自动删除。"""
    key = _validate_key(key)
    if not suffix:
        suffix = os.path.splitext(key)[1]
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(read_bytes(key))
        yield tmp_path
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
