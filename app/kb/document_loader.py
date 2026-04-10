"""
文档加载与三级分块（chunk_id 含 kb_scope，避免跨库冲突）。
采用三组 RecursiveCharacterTextSplitter 进行分块，分别对应 L1/L2/L3 层级。
分块是自上而下嵌套的：
1. 整页文本 → Level 1 切分，得到若干 L1 块；每个 L1 有独立 chunk_id，parent_chunk_id 为空，root_chunk_id 指向自己。
2. 每个 L1 块 → Level 2 切分，得到若干 L2 块；每个 L2 有独立 chunk_id，parent_chunk_id 指向 L1 块，root_chunk_id 指向 L1 块。
3. 每个 L2 块 → Level 3 切分，得到若干 L3 块；每个 L3 有独立 chunk_id，parent_chunk_id 指向 L2 块，root_chunk_id 指向 L1 块。
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List

from langchain_community.document_loaders import Docx2txtLoader, PyPDFLoader, UnstructuredExcelLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter


def _filename_fingerprint(filename: str) -> str:
    """
    使用 SHA256 构建文件名指纹，用于构建 chunk_id
    :param filename: 文件名
    :return: 文件名指纹
    """
    return hashlib.sha256(filename.encode("utf-8")).hexdigest()[:16]


class DocumentLoader:
    def __init__(self, chunk_size: int = 500, chunk_overlap: int = 50) -> None:
        """
        初始化分块器
        :param chunk_size: 分块大小
        :param chunk_overlap: 分块重叠大小
        """
        level_1_size = max(1200, chunk_size * 2)
        level_1_overlap = max(240, chunk_overlap * 2)
        level_2_size = max(600, chunk_size)
        level_2_overlap = max(120, chunk_overlap)
        level_3_size = max(300, chunk_size // 2)
        level_3_overlap = max(60, chunk_overlap // 2)

        self._splitter_level_1 = RecursiveCharacterTextSplitter(
            chunk_size=level_1_size,
            chunk_overlap=level_1_overlap,
            add_start_index=True,
            separators=[
                "\n\n",
                "\n",
                "。",
                "！",
                "？",
                ".",
                "!",
                "?",
                "，",
                ",",
                "、",
                ";",
                " ",
                "",
            ],
        )
        self._splitter_level_2 = RecursiveCharacterTextSplitter(
            chunk_size=level_2_size,
            chunk_overlap=level_2_overlap,
            add_start_index=True,
            separators=[
                "\n\n",
                "\n",
                "。",
                "！",
                "？",
                ".",
                "!",
                "?",
                "，",
                ",",
                "、",
                ";",
                " ",
                "",
            ],
        )
        self._splitter_level_3 = RecursiveCharacterTextSplitter(
            chunk_size=level_3_size,
            chunk_overlap=level_3_overlap,
            add_start_index=True,
            separators=[
                "\n\n",
                "\n",
                "。",
                "！",
                "？",
                ".",
                "!",
                "?",
                "，",
                ",",
                "、",
                ";",
                " ",
                "",
            ],
        )

    @staticmethod
    def _build_chunk_id(kb_scope: str, filename: str, page_number: int, level: int, index: int) -> str:
        """
        构建 chunk_id 的唯一标识符
        chunk_id 格式为：{kb_scope}::{文件名 SHA256 前 16 位}::p{页码}::l{层级}::{该层序号}
        同一知识库内、不同文件/页/层都有稳定唯一 id
        :param kb_scope: 知识库范围
        :param filename: 文件名
        :param page_number: 页码
        :param level: 层级
        :param index: 索引
        :return: chunk_id
        """
        fp = _filename_fingerprint(filename)
        return f"{kb_scope}::{fp}::p{page_number}::l{level}::{index}"

    def _split_page_to_three_levels(
        self,
        text: str,
        base_doc: Dict[str, Any],
        page_global_chunk_idx: int,
    ) -> List[Dict[str, Any]]:
        """
        将一页文本进行三级分块
        :param text: 文本
        :param base_doc: 基础文档信息
        :param page_global_chunk_idx: 全局 chunk 索引
        :return: 分块后的文档列表
        """
        if not text:
            return []
        # 获取基础文档信息
        kb_scope = base_doc["kb_scope"]
        # 初始化根块列表
        root_chunks: List[Dict[str, Any]] = []
        # 获取页码和文件名
        page_number = int(base_doc.get("page_number", 0))
        filename = base_doc["filename"]

        # 进行三级分块
        level_1_docs = self._splitter_level_1.create_documents([text], [base_doc])
        level_1_counter = 0
        level_2_counter = 0
        level_3_counter = 0

        # 进行 L1 层级分块
        for level_1_doc in level_1_docs:
            # 获取 L1 层级文本
            level_1_text = (level_1_doc.page_content or "").strip()
            if not level_1_text:
                continue
            # 构建 L1 层级 chunk_id
            level_1_id = self._build_chunk_id(kb_scope, filename, page_number, 1, level_1_counter)
            level_1_counter += 1
            # 构建 L1 层级 chunk
            level_1_chunk = {
                **base_doc,
                "text": level_1_text,
                "chunk_id": level_1_id,
                "parent_chunk_id": "",  # 根块的 parent_chunk_id 为空
                "root_chunk_id": level_1_id, # 根块的 root_chunk_id 指向自己
                "chunk_level": 1,
                "chunk_idx": page_global_chunk_idx,
            }
            page_global_chunk_idx += 1 # 更新全局 chunk 索引
            root_chunks.append(level_1_chunk)

            # 进行 L2 层级分块
            level_2_docs = self._splitter_level_2.create_documents([level_1_text], [base_doc])
            for level_2_doc in level_2_docs:
                # 获取 L2 层级文本
                level_2_text = (level_2_doc.page_content or "").strip()
                if not level_2_text:
                    continue
                # 构建 L2 层级 chunk_id
                level_2_id = self._build_chunk_id(kb_scope, filename, page_number, 2, level_2_counter)
                level_2_counter += 1 # 更新 L2 层级计数器

                # 构建 L2 层级 chunk
                level_2_chunk = {
                    **base_doc,
                    "text": level_2_text,
                    "chunk_id": level_2_id,
                    "parent_chunk_id": level_1_id, # 父块为 L1 层级块
                    "root_chunk_id": level_1_id, # 根块为 L1 层级块
                    "chunk_level": 2,
                    "chunk_idx": page_global_chunk_idx, # 更新全局 chunk 索引
                }
                page_global_chunk_idx += 1 # 更新全局 chunk 索引    
                root_chunks.append(level_2_chunk)

                # 进行 L3 层级分块
                level_3_docs = self._splitter_level_3.create_documents([level_2_text], [base_doc])
                for level_3_doc in level_3_docs:
                    # 获取 L3 层级文本
                    level_3_text = (level_3_doc.page_content or "").strip()
                    if not level_3_text:
                        continue
                    # 构建 L3 层级 chunk_id
                    level_3_id = self._build_chunk_id(kb_scope, filename, page_number, 3, level_3_counter)
                    level_3_counter += 1 # 更新 L3 层级计数器
                    # 构建 L3 层级 chunk
                    root_chunks.append(
                        {
                            **base_doc,
                            "text": level_3_text,
                            "chunk_id": level_3_id,
                            "parent_chunk_id": level_2_id, # 父块为 L2 层级块
                            "root_chunk_id": level_1_id, # 根块为 L1 层级块
                            "chunk_level": 3,
                            "chunk_idx": page_global_chunk_idx, # 更新全局 chunk 索引
                        }
                    )
                    page_global_chunk_idx += 1 # 更新全局 chunk 索引

        # 返回根块列表
        return root_chunks

    def load_document(self, file_path: str, filename: str, kb_scope: str) -> list[dict]:
        """
        加载文档，并进行三级分块
        :param file_path: 文件路径
        :param filename: 文件名
        :param kb_scope: 知识库范围
        :return: 分块后的文档列表
        """
        # 根据文件名确定文档类型
        file_lower = filename.lower()

        if file_lower.endswith(".pdf"):
            doc_type = "PDF"
            loader = PyPDFLoader(file_path)
        elif file_lower.endswith((".docx", ".doc")):
            doc_type = "Word"
            loader = Docx2txtLoader(file_path)
        elif file_lower.endswith((".xlsx", ".xls")):
            doc_type = "Excel"
            loader = UnstructuredExcelLoader(file_path)
        else:
            raise ValueError(f"不支持的文件类型: {filename}")

        # 加载文档
        raw_docs = loader.load()
        documents: list[dict] = []
        page_global_chunk_idx = 0
        for doc in raw_docs:
            # 构建每一页的基础文档信息
            base_doc = {
                "kb_scope": kb_scope,
                "filename": filename,
                "file_path": file_path,
                "file_type": doc_type,
                "page_number": doc.metadata.get("page", 0),
            }
            # 进行三级分块
            page_chunks = self._split_page_to_three_levels(
                text=(doc.page_content or "").strip(),
                base_doc=base_doc,
                page_global_chunk_idx=page_global_chunk_idx,
            )
            # 更新全局 chunk 索引
            page_global_chunk_idx += len(page_chunks)
            # 将分块后的文档添加到结果列表
            documents.extend(page_chunks)
        # 返回分块后的文档列表
        return documents
