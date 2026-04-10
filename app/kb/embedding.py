"""
全局 DashScope 兼容嵌入 + BM25 稀疏向量。
用于将加载文档中的文本转换为密集向量和稀疏向量。
"""

from __future__ import annotations

import math
import re
from collections import Counter

import requests

from app.settings import settings


class EmbeddingService:
    """密集向量（HTTP）+ BM25 稀疏向量。"""

    def __init__(self) -> None:
        """
        初始化 EmbeddingService
        :return: None
        """
        self.base_url = (settings.EMBEDDING_BASE_URL or "").strip().rstrip("/")
        self.embedder = (settings.EMBEDDING_MODEL or "").strip()
        self.api_key = (settings.EMBEDDING_API_KEY or "").strip()
        self.embedding_dim = max(1, int(settings.EMBEDDING_DIM or 1024))
        self.k1 = 1.5
        self.b = 0.75
        self._vocab: dict[str, int] = {}
        self._vocab_counter = 0
        self._doc_freq: Counter[str] = Counter()
        self._total_docs = 0
        self._avg_doc_len = 1.0

    def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        """
        获取密集向量, 配置中设置的 Embedding 模型，使用 HTTP 请求获取密集向量。
        :param texts: 文本列表
        :return: 密集向量列表
        """
        if not self.api_key:
            raise ValueError("未配置 EMBEDDING_API_KEY")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        data: dict = {
            "model": self.embedder,
            "input": texts,
            "encoding_format": "float",
        }
        model = (self.embedder or "").lower()
        if "text-embedding-v3" in model or "text-embedding-v4" in model:
            data["dimensions"] = self.embedding_dim
        url = f"{self.base_url}/embeddings"
        response = requests.post(url, headers=headers, json=data, timeout=120)
        response.raise_for_status()
        result = response.json()
        return [item["embedding"] for item in result["data"]]

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
        训练语料库
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
        获取稀疏向量，使用 BM25 算法计算稀疏向量。
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

    def get_all_embeddings(self, texts: list[str]) -> tuple[list[list[float]], list[dict[int, float]]]:
        """
        获取密集向量和稀疏向量
        :param texts: 文本列表
        :return: 密集向量和稀疏向量
        """
        dense = self.get_embeddings(texts)
        sparse = self.get_sparse_embeddings(texts)
        return dense, sparse
