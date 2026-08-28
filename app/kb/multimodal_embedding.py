"""
多模态嵌入服务，支持文本和图片的向量化。
使用 DashScope SDK 调用 qwen3-vl-embedding 模型，支持文本嵌入和图片嵌入。
"""

from __future__ import annotations

import base64
import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable

import dashscope
from dashscope import MultiModalEmbedding
from http import HTTPStatus
from loguru import logger

from app.settings import settings


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
        
        # BM25 参数
        self.k1 = 1.5
        self.b = 0.75
        self._vocab: dict[str, int] = {}
        self._vocab_counter = 0
        self._doc_freq: Counter[str] = Counter()
        self._total_docs = 0
        self._avg_doc_len = 1.0

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
        
        try:
            # 准备输入数据
            input_data = [{"text": text} for text in texts]

            call_kwargs: dict[str, Any] = {}
            if request_timeout is not None:
                call_kwargs["request_timeout"] = int(request_timeout)

            # 调用 DashScope API
            resp = MultiModalEmbedding.call(
                model=self.model,
                input=input_data,
                dimension=self.embedding_dim,
                **call_kwargs,
            )
            
            # 检查响应状态
            if resp.status_code != HTTPStatus.OK:
                raise RuntimeError(f"DashScope API 调用失败: {resp.code} - {resp.message}")
            
            # 提取嵌入向量
            embeddings = []
            for item in resp.output.get("embeddings", []):
                if "embedding" in item:
                    embeddings.append(item["embedding"])
            
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
                    # 准备输入数据
                    input_data = []
                    
                    # 判断是本地文件还是URL
                    if os.path.exists(image_path):
                        # 本地文件，使用 file:// 协议
                        # 注意：DashScope 可能需要上传或使用 base64
                        # 这里先尝试直接使用路径
                        input_data.append({"image": f"file://{os.path.abspath(image_path)}"})
                    elif image_path.startswith(("http://", "https://")):
                        # URL
                        input_data.append({"image": image_path})
                    else:
                        # 尝试作为本地路径
                        input_data.append({"image": image_path})

                    call_kwargs: dict[str, Any] = {}
                    if request_timeout is not None:
                        call_kwargs["request_timeout"] = int(request_timeout)

                    # 调用 DashScope API
                    resp = MultiModalEmbedding.call(
                        model=self.model,
                        input=input_data,
                        dimension=self.embedding_dim,
                        **call_kwargs,
                    )
                    
                    # 检查响应状态
                    if resp.status_code != HTTPStatus.OK:
                        logger.warning(f"Failed to generate embedding for image {image_path}: {resp.code} - {resp.message}")
                        # 如果失败，使用零向量
                        embeddings.append([0.0] * self.embedding_dim)
                        continue
                    
                    # 提取嵌入向量
                    if resp.output.get("embeddings") and len(resp.output["embeddings"]) > 0:
                        embedding = resp.output["embeddings"][0].get("embedding", [])
                        embeddings.append(embedding)
                        logger.debug(f"Generated embedding for image {image_path}")
                    else:
                        logger.warning(f"No embedding returned for image {image_path}")
                        embeddings.append([0.0] * self.embedding_dim)
                        
                except Exception as e:
                    logger.warning(f"Failed to generate embedding for image {image_path}: {e}")
                    # 如果失败，使用零向量
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

    def tokenize(self, text: str) -> list[str]:
        """
        分词
        :param text: 文本
        :return: 分词列表
        """
        text = text.lower()
        tokens: list[str] = []
        chinese_pattern = re.compile(r"[\u4e00-\u9fff]")
        english_pattern = re.compile(r"[a-zA-Z]+")
        i = 0
        while i < len(text):
            char = text[i]
            if chinese_pattern.match(char):
                tokens.append(char)
                i += 1
            elif english_pattern.match(char):
                match = english_pattern.match(text[i:])
                if match:
                    tokens.append(match.group())
                    i += len(match.group())
            else:
                i += 1
        return tokens

    def fit_corpus(self, texts: list[str]) -> None:
        """
        训练语料库（用于 BM25 稀疏向量）
        :param texts: 文本列表
        :return: None
        """
        self._total_docs = len(texts)
        total_len = 0
        for text in texts:
            tokens = self.tokenize(text)
            total_len += len(tokens)
            for token in set(tokens):
                self._doc_freq[token] += 1
                if token not in self._vocab:
                    self._vocab[token] = self._vocab_counter
                    self._vocab_counter += 1
        self._avg_doc_len = total_len / self._total_docs if self._total_docs > 0 else 1.0

    def get_sparse_embedding(self, text: str) -> dict[int, float]:
        """
        获取稀疏向量（BM25 算法）
        :param text: 文本
        :return: 稀疏向量
        """
        tokens = self.tokenize(text)
        doc_len = len(tokens)
        tf = Counter(tokens)
        sparse_vector: dict[int, float] = {}
        for token, freq in tf.items():
            if token not in self._vocab:
                self._vocab[token] = self._vocab_counter
                self._vocab_counter += 1
            idx = self._vocab[token]
            df = self._doc_freq.get(token, 0)
            if df == 0:
                idf = math.log((self._total_docs + 1) / 1)
            else:
                idf = math.log((self._total_docs - df + 0.5) / (df + 0.5) + 1)
            numerator = freq * (self.k1 + 1)
            denominator = freq + self.k1 * (1 - self.b + self.b * doc_len / max(self._avg_doc_len, 1.0))
            score = idf * numerator / denominator
            if score > 0:
                sparse_vector[idx] = float(score)
        return sparse_vector

    def get_sparse_embeddings(self, texts: list[str]) -> list[dict[int, float]]:
        """
        获取稀疏向量列表
        :param texts: 文本列表
        :return: 稀疏向量列表
        """
        return [self.get_sparse_embedding(t) for t in texts]

    def get_all_embeddings(
        self,
        texts: list[str] | None = None,
        image_paths: list[str] | None = None,
    ) -> tuple[list[list[float]], list[dict[int, float]]]:
        """
        获取密集向量和稀疏向量
        :param texts: 文本列表
        :param image_paths: 图片路径列表
        :return: 密集向量和稀疏向量
        """
        dense = []
        sparse = []
        
        if texts:
            dense.extend(self.get_text_embeddings(texts))
            sparse.extend(self.get_sparse_embeddings(texts))
        
        if image_paths:
            dense.extend(self.get_image_embeddings(image_paths))
            # 图片块无文本稀疏向量，占位为空字典
            sparse.extend([{} for _ in image_paths])
        
        return dense, sparse


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
