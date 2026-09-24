"""
多模态嵌入服务，支持文本和图片的向量化。
使用 DashScope SDK 调用 qwen3-vl-embedding 模型，支持文本嵌入和图片嵌入。
"""

from __future__ import annotations

import os
import random
import threading
import time
from typing import Any, Callable

import dashscope
from dashscope import MultiModalEmbedding
from http import HTTPStatus
from loguru import logger

from app.settings import settings


class EmbeddingThrottledError(RuntimeError):
    """嵌入服务限流（DashScope 429 / Throttling.*）：可退避重试，重试耗尽后由上层判为失败。"""


class EmbeddingConcurrencyTimeoutError(RuntimeError):
    """等待全局嵌入并发额度超时（说明在途嵌入调用过多）。"""


_gate_lock = threading.Lock()
_gate: threading.BoundedSemaphore | None = None


def _concurrency_gate() -> threading.BoundedSemaphore:
    """
    全局嵌入并发信号量（跨所有上传/实验线程共享，懒加载）。
    限制同时打在服务商侧的在途请求数，从源头降低触发限流的概率。
    :return: 信号量实例
    """
    global _gate
    if _gate is None:
        with _gate_lock:
            if _gate is None:
                limit = max(1, int(getattr(settings, "EMBEDDING_MAX_CONCURRENCY", 4) or 4))
                _gate = threading.BoundedSemaphore(limit)
    return _gate


class _embedding_slot:
    """上下文管理器：占用一个全局嵌入并发额度；超时未取得则抛 EmbeddingConcurrencyTimeoutError。"""

    def __enter__(self) -> "_embedding_slot":
        wait = max(1, int(getattr(settings, "EMBEDDING_CONCURRENCY_WAIT_SECONDS", 120) or 120))
        if not _concurrency_gate().acquire(timeout=wait):
            raise EmbeddingConcurrencyTimeoutError(f"等待嵌入并发额度超过 {wait} 秒，服务商侧在途请求过多")
        return self

    def __exit__(self, *_exc: Any) -> bool:
        _concurrency_gate().release()
        return False


# 限流/配额类错误码与关键字（DashScope 返回 resp.code / 兼容模式返回 JSON error）
_THROTTLE_CODES = {
    "Throttling",
    "Throttling.User",
    "Throttling.Resource",
    "Throttling.IP",
    "Throttling.Group",
    "RequestLimitExceeded",
    "LimitExceeded",
    "Arrearage",
    "429",
}
_THROTTLE_HINTS = ("throttl", "rate limit", "ratelimit", "too many requests", " 429", "qps", "quota", "限流")
# 网络抖动类异常（按类名匹配，兼容 requests / httpx / openai 各层封装）
_RETRYABLE_EXC_NAMES = {
    "ConnectionError",
    "ConnectionResetError",
    "RemoteDisconnected",
    "Timeout",
    "TimeoutError",
    "ReadTimeout",
    "ConnectTimeout",
    "SSLError",
    "ChunkedEncodingError",
    "ProtocolError",
    "IncompleteRead",
    "APIConnectionError",
    "APITimeoutError",
    "InternalServerError",
    "ServiceUnavailableError",
}


def is_throttle_error(code: Any, message: Any, status_code: Any = None) -> bool:
    """
    判断是否为服务商限流/配额错误（用于区分「限流」与「真失败」，前端可提示自动重试中）。
    :param code: DashScope 返回码或 HTTP error code
    :param message: 错误消息
    :param status_code: HTTP 状态码
    :return: 是否属于限流
    """
    try:
        if status_code is not None and int(status_code) == 429:
            return True
    except (TypeError, ValueError):
        pass
    if str(code or "").strip() in _THROTTLE_CODES:
        return True
    blob = f"{code or ''} {message or ''}".lower()
    return any(hint in blob for hint in _THROTTLE_HINTS)


def _is_retryable(exc: Exception) -> bool:
    """是否为可重试异常（限流、并发额度等待超时、网络抖动）。"""
    if isinstance(exc, (EmbeddingThrottledError, EmbeddingConcurrencyTimeoutError)):
        return True
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    if type(exc).__name__ in _RETRYABLE_EXC_NAMES:
        return True
    return is_throttle_error(None, str(exc))


def _call_with_retry(fn: Callable[[], Any], describe: str) -> Any:
    """
    带指数退避 + 抖动的重试执行（仅对限流/网络抖动类异常重试）。
    :param fn: 实际调用（内部已含并发额度占用与状态码校验）
    :param describe: 日志描述
    :return: fn 的返回值
    """
    max_retries = max(0, int(getattr(settings, "KB_UPLOAD_EMBEDDING_MAX_RETRIES", 3) or 0))
    base = max(0.1, float(getattr(settings, "KB_UPLOAD_EMBEDDING_RETRY_BASE_SECONDS", 1.0) or 1.0))
    cap = max(base, float(getattr(settings, "KB_UPLOAD_EMBEDDING_RETRY_MAX_SECONDS", 8.0) or 8.0))
    attempt = 0
    while True:
        try:
            with _embedding_slot():
                return fn()
        except Exception as e:  # noqa: BLE001
            if attempt >= max_retries or not _is_retryable(e):
                raise
            attempt += 1
            # 抖动 0.7~1.3 倍，避免多线程同时重试形成新的尖峰
            delay = min(cap, base * (2 ** (attempt - 1))) * (0.7 + random.random() * 0.6)
            logger.warning("{} 第 {}/{} 次重试（{:.1f}s 后）: {}", describe, attempt, max_retries, delay, e)
            time.sleep(delay)


class MultimodalEmbeddingService:
    """多模态嵌入服务，支持文本和图片的向量化"""

    def __init__(self) -> None:
        """
        初始化 MultimodalEmbeddingService
        :return: None
        """
        self.api_key = (settings.EMBEDDING_API_KEY or "").strip()
        self.model = (settings.EMBEDDING_MODEL or "qwen3-vl-embedding").strip()
        self.embedding_dim = max(1, int(settings.EMBEDDING_DIM or 1536))
        
        # 设置 DashScope API Key
        if self.api_key:
            dashscope.api_key = self.api_key

    def get_text_embeddings(self, texts: list[str], request_timeout: int | None = None) -> list[list[float]]:
        """
        获取文本的密集向量
        :param texts: 文本列表
        :param request_timeout: 单次 HTTP 调用超时（秒，None 用 SDK 默认）；SDK 经 request_timeout kwarg 透传到 requests
        :return: 密集向量列表
        """
        if not self.api_key:
            raise ValueError("未配置 EMBEDDING_API_KEY")
        
        if not texts:
            return []
        
        # 准备输入数据
        input_data = [{"text": text} for text in texts]

        call_kwargs: dict[str, Any] = {}
        if request_timeout is not None:
            call_kwargs["request_timeout"] = int(request_timeout)

        def _call() -> list[list[float]]:
            # 调用 DashScope API
            resp = MultiModalEmbedding.call(
                model=self.model,
                input=input_data,
                dimension=self.embedding_dim,
                **call_kwargs,
            )

            # 检查响应状态（限流单独归类，便于重试与前端提示）
            if resp.status_code != HTTPStatus.OK:
                if is_throttle_error(resp.code, resp.message, resp.status_code):
                    raise EmbeddingThrottledError(f"DashScope 限流: {resp.code} - {resp.message}")
                raise RuntimeError(f"DashScope API 调用失败: {resp.code} - {resp.message}")

            # 提取嵌入向量
            embeddings: list[list[float]] = []
            for item in resp.output.get("embeddings", []):
                if "embedding" in item:
                    embeddings.append(item["embedding"])
            return embeddings

        try:
            embeddings = _call_with_retry(_call, f"文本嵌入（{len(texts)} 条）")
            logger.info(f"Generated {len(embeddings)} text embeddings with dimension {len(embeddings[0]) if embeddings else 0}")
            return embeddings
        except Exception as e:
            logger.error(f"Failed to generate text embeddings: {e}")
            raise

    def get_image_embeddings(
        self,
        image_paths: list[str],
        request_timeout: int | None = None,
        tick_cb: Callable[[], None] | None = None,
    ) -> list[list[float]]:
        """
        获取图片的密集向量（DashScope 一次只能处理一张图片，逐张串行）
        :param image_paths: 图片路径列表（支持本地路径或URL）
        :param request_timeout: 单次 HTTP 调用超时（秒，None 用 SDK 默认）；SDK 经 request_timeout kwarg 透传到 requests
        :param tick_cb: 每张图片处理前调用（用于协作式取消/超时检查，抛异常即中止）
        :return: 密集向量列表
        """
        if not self.api_key:
            raise ValueError("未配置 EMBEDDING_API_KEY")
        
        if not image_paths:
            return []
        
        embeddings = []
        
        try:
            # 为每张图片生成向量（DashScope 一次只能处理一张图片）
            for image_path in image_paths:
                if tick_cb is not None:
                    tick_cb()
                try:
                    # 准备输入数据；本地文件用 file:// 协议，URL 原样透传
                    if os.path.exists(image_path):
                        input_data = [{"image": f"file://{os.path.abspath(image_path)}"}]
                    else:
                        input_data = [{"image": image_path}]

                    call_kwargs: dict[str, Any] = {}
                    if request_timeout is not None:
                        call_kwargs["request_timeout"] = int(request_timeout)

                    def _call() -> list[float]:
                        resp = MultiModalEmbedding.call(
                            model=self.model,
                            input=input_data,
                            dimension=self.embedding_dim,
                            **call_kwargs,
                        )
                        if resp.status_code != HTTPStatus.OK:
                            if is_throttle_error(resp.code, resp.message, resp.status_code):
                                raise EmbeddingThrottledError(f"DashScope 限流: {resp.code} - {resp.message}")
                            logger.warning(f"Failed to generate embedding for image {image_path}: {resp.code} - {resp.message}")
                            return [0.0] * self.embedding_dim
                        items = resp.output.get("embeddings") or []
                        if items:
                            return items[0].get("embedding", [])
                        logger.warning(f"No embedding returned for image {image_path}")
                        return [0.0] * self.embedding_dim

                    embedding = _call_with_retry(_call, f"图片嵌入 {os.path.basename(image_path)}")
                    embeddings.append(embedding)
                    logger.debug(f"Generated embedding for image {image_path}")

                except (EmbeddingThrottledError, EmbeddingConcurrencyTimeoutError):
                    # 限流重试耗尽：向上抛出，避免用零向量污染检索索引（旧文档保持原样，用户可重传）
                    raise
                except Exception as e:
                    logger.warning(f"Failed to generate embedding for image {image_path}: {e}")
                    # 其它失败（如单图格式异常）沿用零向量兜底，保证整篇文档其余内容可入库
                    embeddings.append([0.0] * self.embedding_dim)

            logger.info(f"Generated {len(embeddings)} image embeddings")
            return embeddings

        except Exception as e:
            logger.error(f"Failed to generate image embeddings: {e}")
            raise

    def get_multimodal_fusion_embeddings(
        self,
        texts: list[str] | None = None,
        image_paths: list[str] | None = None,
    ) -> list[list[float]]:
        """
        获取多模态融合向量（文本 + 图片融合成一个向量）
        :param texts: 文本列表
        :param image_paths: 图片路径列表
        :return: 融合向量列表
        """
        if not self.api_key:
            raise ValueError("未配置 EMBEDDING_API_KEY")
        
        embeddings = []
        
        try:
            # 准备输入数据
            input_data = []
            
            if texts:
                for text in texts:
                    input_data.append({"text": text})
            
            if image_paths:
                for image_path in image_paths:
                    if os.path.exists(image_path):
                        input_data.append({"image": f"file://{os.path.abspath(image_path)}"})
                    else:
                        input_data.append({"image": image_path})
            
            if not input_data:
                return []
            
            # 调用 DashScope API，启用融合
            resp = MultiModalEmbedding.call(
                model=self.model,
                input=input_data,
                enable_fusion=True,
                dimension=self.embedding_dim,
            )
            
            # 检查响应状态
            if resp.status_code != HTTPStatus.OK:
                raise RuntimeError(f"DashScope API 调用失败: {resp.code} - {resp.message}")
            
            # 提取嵌入向量
            if resp.output.get("embeddings") and len(resp.output["embeddings"]) > 0:
                embedding = resp.output["embeddings"][0].get("embedding", [])
                embeddings.append(embedding)
                logger.info(f"Generated multimodal fusion embedding with dimension {len(embedding)}")
            
            return embeddings
            
        except Exception as e:
            logger.error(f"Failed to generate multimodal fusion embeddings: {e}")
            raise

    def get_multimodal_embeddings(
        self,
        texts: list[str] | None = None,
        image_paths: list[str] | None = None,
    ) -> list[list[float]]:
        """
        获取多模态的密集向量（文本 + 图片分别生成向量）
        :param texts: 文本列表
        :param image_paths: 图片路径列表
        :return: 密集向量列表
        """
        embeddings = []
        
        if texts:
            text_embeddings = self.get_text_embeddings(texts)
            embeddings.extend(text_embeddings)
        
        if image_paths:
            image_embeddings = self.get_image_embeddings(image_paths)
            embeddings.extend(image_embeddings)
        
        return embeddings


# 全局多模态嵌入服务实例
_multimodal_embedding_service = None


def get_multimodal_embedding_service() -> MultimodalEmbeddingService:
    """
    获取全局多模态嵌入服务实例
    :return: MultimodalEmbeddingService
    """
    global _multimodal_embedding_service
    if _multimodal_embedding_service is None:
        _multimodal_embedding_service = MultimodalEmbeddingService()
    return _multimodal_embedding_service
