"""
多模态文档加载与图文解析，扩展原有三级分块功能，支持图片提取和存储。
采用三组 RecursiveCharacterTextSplitter 进行分块，分别对应 L1/L2/L3 层级。
图片作为独立的 L4 块处理，与文本块通过位置关联。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF
from langchain_community.document_loaders import Docx2txtLoader, PyPDFLoader, UnstructuredExcelLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from loguru import logger

from app.kb.multimodal_embedding import get_multimodal_embedding_service
from app.settings import settings


def _filename_fingerprint(filename: str) -> str:
    """
    使用 SHA256 构建文件名指纹，用于构建 chunk_id
    :param filename: 文件名
    :return: 文件名指纹
    """
    return hashlib.sha256(filename.encode("utf-8")).hexdigest()[:16]


class MultimodalDocumentLoader:
    def __init__(self, chunk_size: int = 500, chunk_overlap: int = 50) -> None:
        """
        初始化多模态分块器
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
                "\\n\\n",
                "\\n",
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
                "\\n\\n",
                "\\n",
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
                "\\n\\n",
                "\\n",
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
        
        # 初始化嵌入服务
        self.embedding_service = get_multimodal_embedding_service()

    @staticmethod
    def _build_chunk_id(kb_scope: str, filename: str, page_number: int, level: int, index: int, img_idx: int = 0) -> str:
        """
        构建 chunk_id 的唯一标识符
        chunk_id 格式为：{kb_scope}::{文件名 SHA256 前 16 位}::p{页码}::l{层级}::{该层序号}::i{图片序号}
        :param kb_scope: 知识库范围
        :param filename: 文件名
        :param page_number: 页码
        :param level: 层级
        :param index: 索引
        :param img_idx: 图片序号（仅L4图片块使用）
        :return: chunk_id
        """
        fp = _filename_fingerprint(filename)
        if level == 4:  # 图片块
            return f"{kb_scope}::{fp}::p{page_number}::l{level}::{index}::i{img_idx}"
        return f"{kb_scope}::{fp}::p{page_number}::l{level}::{index}"

    def _extract_images_from_pdf(
        self,
        pdf_path: str,
        user_id: int,
        agent_id: int,
        kb_scope: str,
        filename: str,
    ) -> List[Dict[str, Any]]:
        """
        从 PDF 中提取真实的图片对象（使用 PyMuPDF），并获取图片位置信息
        :param pdf_path: PDF 文件路径
        :param user_id: 用户ID
        :param agent_id: 智能体ID
        :param kb_scope: 知识库范围
        :param filename: 文件名
        :return: 图片信息列表
        """
        images = []
        try:
            # 创建图片存储目录
            images_dir = Path(settings.USER_AGENT_KB_IMAGES_ROOT) / f"user_{user_id}" / str(agent_id) / _filename_fingerprint(filename)
            images_dir.mkdir(parents=True, exist_ok=True)
            
            # 使用 PyMuPDF 打开 PDF
            doc = fitz.open(pdf_path)
            
            # 遍历每一页
            for page_num in range(len(doc)):
                page = doc[page_num]
                
                # 获取页面尺寸
                page_rect = page.rect
                page_height = page_rect.height
                
                # 获取页面上的所有图片
                image_list = page.get_images(full=True)
                
                # 提取每张图片
                for img_index, img_info in enumerate(image_list):
                    try:
                        # 获取图片的 xref（图片对象在 PDF 中的引用）
                        xref = img_info[0]
                        
                        # 提取图片
                        base_image = doc.extract_image(xref)
                        
                        # 获取图片数据
                        image_bytes = base_image["image"]
                        image_ext = base_image["ext"]
                        image_width = base_image["width"]
                        image_height = base_image["height"]
                        
                        # 过滤掉太小的图片（可能是图标、装饰性元素等）
                        if image_width < 100 or image_height < 100:
                            logger.debug(f"Skipping small image: {image_width}x{image_height} on page {page_num + 1}")
                            continue
                        
                        # 获取图片在页面上的位置
                        # PyMuPDF 的 get_image_info() 可以获取图片的位置
                        img_rects = page.get_image_rects(xref)
                        
                        if img_rects:
                            # 使用第一个矩形区域
                            rect = img_rects[0]
                            # PDF坐标系：原点在左下角，y轴向上
                            position_x = int(rect.x0)
                            position_y = int(rect.y0)
                            position_width = int(rect.width)
                            position_height = int(rect.height)
                        else:
                            # 如果无法获取位置，使用默认值
                            position_x = 0
                            position_y = 0
                            position_width = image_width
                            position_height = image_height
                        
                        # 生成图片文件名
                        image_filename = f"page_{page_num + 1:04d}_img_{img_index + 1:04d}.{image_ext}"
                        image_path = images_dir / image_filename
                        
                        # 保存图片
                        with open(image_path, "wb") as f:
                            f.write(image_bytes)
                        
                        # 构建图片信息（page_number 与 PyPDFLoader 的 metadata["page"] 一致：0 起算）
                        image_info = {
                            "kb_scope": kb_scope,
                            "filename": filename,
                            "file_type": "PDF",
                            "page_number": page_num,
                            "stored_path": str(image_path),
                            "width": image_width,
                            "height": image_height,
                            "format": image_ext,
                            "image_index": img_index + 1,
                            # 图片在页面中的位置
                            "position_x": position_x,
                            "position_y": position_y,
                            "position_width": position_width,
                            "position_height": position_height,
                        }
                        images.append(image_info)
                        
                        logger.debug(f"Extracted image {img_index + 1} from page {page_num + 1}: {image_width}x{image_height} at ({position_x}, {position_y})")
                        
                    except Exception as e:
                        logger.warning(f"Failed to extract image {img_index + 1} from page {page_num + 1}: {e}")
                        continue
            
            doc.close()
            logger.info(f"Extracted {len(images)} images from {pdf_path}")
            
        except Exception as e:
            logger.error(f"Failed to extract images from {pdf_path}: {e}")
        
        return images

    def _extract_images_from_docx(
        self,
        docx_path: str,
        user_id: int,
        agent_id: int,
        kb_scope: str,
        filename: str,
    ) -> List[Dict[str, Any]]:
        """
        从 DOCX 中提取图片
        :param docx_path: DOCX 文件路径
        :param user_id: 用户ID
        :param agent_id: 智能体ID
        :param kb_scope: 知识库范围
        :param filename: 文件名
        :return: 图片信息列表
        """
        images = []
        try:
            import docx
            from PIL import Image
            
            # 创建图片存储目录
            images_dir = Path(settings.USER_AGENT_KB_IMAGES_ROOT) / f"user_{user_id}" / str(agent_id) / _filename_fingerprint(filename)
            images_dir.mkdir(parents=True, exist_ok=True)
            
            # 打开 DOCX 文件
            doc = docx.Document(docx_path)
            
            image_idx = 0
            # 遍历文档中的所有段落
            for para_idx, paragraph in enumerate(doc.paragraphs):
                # 遍历段落中的所有运行
                for run_idx, run in enumerate(paragraph.runs):
                    # 检查运行中是否包含图片
                    for rel in run._element.xpath('.//pic:pic'):
                        image_idx += 1
                        
                        # 提取图片数据
                        image_data = None
                        for blip in rel.xpath('.//a:blip'):
                            r_id = blip.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed')
                            if r_id:
                                image_part = doc.part.related_parts[r_id]
                                image_data = image_part.blob
                                break
                        
                        if image_data:
                            # 保存图片
                            image_path = images_dir / f"para_{para_idx:04d}_run_{run_idx:04d}_img_{image_idx:04d}.png"
                            with open(image_path, 'wb') as f:
                                f.write(image_data)
                            
                            # 获取图片信息
                            try:
                                with Image.open(image_path) as img:
                                    width, height = img.size
                                    img_format = img.format.lower() if img.format else "png"
                            except:
                                width, height = 0, 0
                                img_format = "png"
                            
                            # 构建图片信息
                            image_info = {
                                "kb_scope": kb_scope,
                                "filename": filename,
                                "file_type": "Word",
                                "page_number": para_idx + 1,  # 使用段落索引作为页码
                                "stored_path": str(image_path),
                                "width": width,
                                "height": height,
                                "format": img_format,
                                "image_index": image_idx,
                                # Word 文档中的图片位置信息有限
                                "position_x": 0,
                                "position_y": para_idx * 100,  # 估算的垂直位置
                                "position_width": width,
                                "position_height": height,
                            }
                            images.append(image_info)
            
            logger.info(f"Extracted {len(images)} images from {docx_path}")
            
        except ImportError:
            logger.warning("python-docx not available for image extraction from DOCX")
        except Exception as e:
            logger.error(f"Failed to extract images from {docx_path}: {e}")
        
        return images

    @staticmethod
    def _pdf_text_rect_for_l3(page: fitz.Page, text_chunk: dict) -> Optional[fitz.Rect]:
        """在单页上定位 L3 文块的包围盒（与图片 rect 同处 PDF 页面坐标系，y 轴向下）。"""
        raw = (text_chunk.get("text") or "").strip()
        if len(raw) < 2:
            return None
        first_line = raw.splitlines()[0].strip() if raw else ""
        needles: List[str] = []
        if len(first_line) >= 4:
            needles.append(first_line[:240])
        for max_len in (200, 150, 100, 60, 40):
            frag = " ".join(raw[:max_len].split())
            if len(frag) >= 3:
                needles.append(frag)
        seen: set[str] = set()
        for n in needles:
            if n in seen or not n:
                continue
            seen.add(n)
            try:
                hits = page.search_for(n)
            except Exception:
                continue
            if not hits and len(n) > 32:
                try:
                    hits = page.search_for(n[:32])
                except Exception:
                    hits = []
            if hits:
                u = hits[0]
                for h in hits[1:10]:
                    u |= h
                return u
        return None

    @staticmethod
    def _link_image_to_l3_text(img: dict, text_chunk: dict) -> None:
        if "related_text_ids" not in img:
            img["related_text_ids"] = []
        img["related_text_ids"].append(text_chunk["chunk_id"])
        img["parent_chunk_id"] = text_chunk["chunk_id"]
        if "related_image_ids" not in text_chunk:
            text_chunk["related_image_ids"] = []
        text_chunk["related_image_ids"].append(img.get("chunk_id", ""))

    def _associate_page_by_order(
        self,
        page_l3: List[Dict[str, Any]],
        page_images: List[Dict[str, Any]],
    ) -> None:
        """按 L3 阅读顺序与图片纵向顺序配对，不混用字符下标与页面纵坐标量纲。"""
        if not page_l3 or not page_images:
            return
        texts_sorted = sorted(
            page_l3,
            key=lambda c: (int(c.get("position_start", 0) or 0), int(c.get("position_end", 0) or 0)),
        )
        imgs_sorted = sorted(
            page_images,
            key=lambda m: (float(m.get("position_y", 0) or 0.0),),
        )
        for img, ch in zip(imgs_sorted, texts_sorted):
            self._link_image_to_l3_text(img, ch)

    def _associate_page_pdf_geometry(
        self,
        page: fitz.Page,
        page_l3: List[Dict[str, Any]],
        page_images: List[Dict[str, Any]],
    ) -> None:
        """同一 PDF 页：用 L3 与图片的 PDF 页面坐标，优先将「在图片上方」的最近 L3 关联到该图。"""
        l3_with_rect: List[Tuple[dict, fitz.Rect]] = []
        for tc in page_l3:
            r = self._pdf_text_rect_for_l3(page, tc)
            if r is not None:
                l3_with_rect.append((tc, r))

        if not l3_with_rect:
            self._associate_page_by_order(page_l3, page_images)
            return

        for img in page_images:
            img_top = float(img.get("position_y", 0) or 0)
            img_h = float(img.get("position_height", 0) or 0)
            img_cy = img_top + 0.5 * img_h

            above: List[Tuple[dict, fitz.Rect]] = [
                (tc, r) for tc, r in l3_with_rect if r.y1 <= img_top + 0.5
            ]
            if above:
                best_tc = max(above, key=lambda x: x[1].y1)[0]
                self._link_image_to_l3_text(img, best_tc)
            else:
                def _d(item: Tuple[dict, fitz.Rect]) -> float:
                    _tr, r = item
                    tcy = 0.5 * (r.y0 + r.y1)
                    return (tcy - img_cy) ** 2

                best_tc = min(l3_with_rect, key=_d)[0]
                self._link_image_to_l3_text(img, best_tc)

    def _associate_text_with_images(
        self,
        text_chunks: List[Dict[str, Any]],
        images: List[Dict[str, Any]],
        *,
        file_path: Optional[str] = None,
        doc_type: str = "",
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        建立 L3 文本块与图片的关联关系（检索侧 related_text_ids 以 L3 为主）。
        PDF 使用 page.search_for 得到的 bbox 与图片位置同一坐标系；无 bbox 时按阅读/纵向序配对。
        非 PDF 同页用顺序配对，避免将字符下标与纵坐标比较。
        """
        if not text_chunks or not images:
            return text_chunks, images

        def _l3_only(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            return [c for c in chunks if int(c.get("chunk_level", 0) or 0) == 3]

        text_by_page: Dict[int, List[dict]] = {}
        for chunk in text_chunks:
            p = int(chunk.get("page_number", 0) or 0)
            if p not in text_by_page:
                text_by_page[p] = []
            text_by_page[p].append(chunk)

        images_by_page: Dict[int, List[dict]] = {}
        for im in images:
            p = int(im.get("page_number", 0) or 0)
            if p not in images_by_page:
                images_by_page[p] = []
            images_by_page[p].append(im)

        use_pdf = (
            (doc_type or "").upper() == "PDF"
            and file_path
            and str(file_path).lower().endswith(".pdf")
            and Path(file_path).is_file()
        )

        if use_pdf:
            try:
                doc = fitz.open(file_path)
            except Exception as e:
                logger.warning("无法打开 PDF 作图文关联，同页将按顺序配对: {}", e)
                doc = None
            if doc is not None:
                try:
                    for pno in sorted(set(text_by_page) & set(images_by_page)):
                        if pno < 0 or pno >= doc.page_count:
                            continue
                        pl3 = _l3_only(text_by_page[pno])
                        pimgs = images_by_page[pno]
                        if not pl3 or not pimgs:
                            continue
                        self._associate_page_pdf_geometry(doc[pno], pl3, pimgs)
                finally:
                    doc.close()
            if doc is None:
                for pno in sorted(set(text_by_page) & set(images_by_page)):
                    pl3 = _l3_only(text_by_page[pno])
                    pimgs = images_by_page[pno]
                    if pl3 and pimgs:
                        self._associate_page_by_order(pl3, pimgs)
            return text_chunks, images

        for pno in sorted(set(text_by_page) & set(images_by_page)):
            pl3 = _l3_only(text_by_page[pno])
            pimgs = images_by_page[pno]
            if pl3 and pimgs:
                self._associate_page_by_order(pl3, pimgs)
        return text_chunks, images

    def _create_image_chunks(
        self,
        images: List[Dict[str, Any]],
        base_doc: Dict[str, Any],
        chunk_idx_start: int,
    ) -> List[Dict[str, Any]]:
        """
        为图片创建 L4 块
        :param images: 图片信息列表
        :param base_doc: 基础文档信息
        :param chunk_idx_start: 起始chunk索引
        :return: 图片块列表
        """
        image_chunks = []
        
        for img_idx, image_info in enumerate(images):
            # 生成图片块ID
            image_chunk_id = self._build_chunk_id(
                base_doc["kb_scope"],
                base_doc["filename"],
                image_info.get("page_number", 0),
                4,  # L4 图片块
                chunk_idx_start + img_idx,
                img_idx,
            )
            
            # 构建图片块
            image_chunk = {
                **base_doc,
                "text": "",
                "content_type": "image",
                "image_path": image_info.get("stored_path", ""),
                "chunk_id": image_chunk_id,
                "parent_chunk_id": image_info.get("parent_chunk_id", ""),
                "root_chunk_id": base_doc.get("root_chunk_id", ""),
                "chunk_level": 4,
                "chunk_idx": chunk_idx_start + img_idx,
                "page_number": image_info.get("page_number", 0),
                # 图片位置信息
                "position_start": 0,
                "position_end": 0,
                "image_position_x": image_info.get("position_x", 0),
                "image_position_y": image_info.get("position_y", 0),
                "image_width": image_info.get("position_width", 0),
                "image_height": image_info.get("position_height", 0),
                "image_metadata": {
                    "width": image_info.get("width", 0),
                    "height": image_info.get("height", 0),
                    "format": image_info.get("format", "png"),
                    "image_index": image_info.get("image_index", 0),
                },
            }
            
            # 添加关联信息
            if image_info.get("related_text_ids"):
                image_chunk["related_text_ids"] = image_info["related_text_ids"]
            
            image_chunks.append(image_chunk)
        
        return image_chunks

    def _split_page_to_three_levels(
        self,
        text: str,
        base_doc: Dict[str, Any],
        page_global_chunk_idx: int,
        page_text_start: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        将一页文本进行三级分块
        :param text: 文本
        :param base_doc: 基础文档信息
        :param page_global_chunk_idx: 全局 chunk 索引
        :param page_text_start: 页面文本的起始位置
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
            
            # 获取 L1 块在原始文本中的位置
            l1_start_idx = level_1_doc.metadata.get("start_index", 0)
            l1_end_idx = l1_start_idx + len(level_1_text)
            
            # 构建 L1 层级 chunk_id
            level_1_id = self._build_chunk_id(kb_scope, filename, page_number, 1, level_1_counter)
            level_1_counter += 1
            
            # 构建 L1 层级 chunk
            level_1_chunk = {
                **base_doc,
                "text": level_1_text,
                "content_type": "text",
                "chunk_id": level_1_id,
                "parent_chunk_id": "",  # 根块的 parent_chunk_id 为空
                "root_chunk_id": level_1_id,  # 根块的 root_chunk_id 指向自己
                "chunk_level": 1,
                "chunk_idx": page_global_chunk_idx,
                # 文本位置信息
                "position_start": page_text_start + l1_start_idx,
                "position_end": page_text_start + l1_end_idx,
            }
            page_global_chunk_idx += 1  # 更新全局 chunk 索引
            root_chunks.append(level_1_chunk)

            # 进行 L2 层级分块
            level_2_docs = self._splitter_level_2.create_documents([level_1_text], [base_doc])
            for level_2_doc in level_2_docs:
                # 获取 L2 层级文本
                level_2_text = (level_2_doc.page_content or "").strip()
                if not level_2_text:
                    continue
                
                # 获取 L2 块在原始文本中的位置
                l2_start_idx = level_2_doc.metadata.get("start_index", 0)
                l2_end_idx = l2_start_idx + len(level_2_text)
                
                # 构建 L2 层级 chunk_id
                level_2_id = self._build_chunk_id(kb_scope, filename, page_number, 2, level_2_counter)
                level_2_counter += 1  # 更新 L2 层级计数器

                # 构建 L2 层级 chunk
                level_2_chunk = {
                    **base_doc,
                    "text": level_2_text,
                    "content_type": "text",
                    "chunk_id": level_2_id,
                    "parent_chunk_id": level_1_id,  # 父块为 L1 层级块
                    "root_chunk_id": level_1_id,  # 根块为 L1 层级块
                    "chunk_level": 2,
                    "chunk_idx": page_global_chunk_idx,  # 更新全局 chunk 索引
                    # 文本位置信息
                    "position_start": page_text_start + l1_start_idx + l2_start_idx,
                    "position_end": page_text_start + l1_start_idx + l2_end_idx,
                }
                page_global_chunk_idx += 1  # 更新全局 chunk 索引
                root_chunks.append(level_2_chunk)

                # 进行 L3 层级分块
                level_3_docs = self._splitter_level_3.create_documents([level_2_text], [base_doc])
                for level_3_doc in level_3_docs:
                    # 获取 L3 层级文本
                    level_3_text = (level_3_doc.page_content or "").strip()
                    if not level_3_text:
                        continue
                    
                    # 获取 L3 块在原始文本中的位置
                    l3_start_idx = level_3_doc.metadata.get("start_index", 0)
                    l3_end_idx = l3_start_idx + len(level_3_text)
                    
                    # 构建 L3 层级 chunk_id
                    level_3_id = self._build_chunk_id(kb_scope, filename, page_number, 3, level_3_counter)
                    level_3_counter += 1  # 更新 L3 层级计数器
                    
                    # 构建 L3 层级 chunk
                    root_chunks.append(
                        {
                            **base_doc,
                            "text": level_3_text,
                            "content_type": "text",
                            "chunk_id": level_3_id,
                            "parent_chunk_id": level_2_id,  # 父块为 L2 层级块
                            "root_chunk_id": level_1_id,  # 根块为 L1 层级块
                            "chunk_level": 3,
                            "chunk_idx": page_global_chunk_idx,  # 更新全局 chunk 索引
                            # 文本位置信息
                            "position_start": page_text_start + l1_start_idx + l2_start_idx + l3_start_idx,
                            "position_end": page_text_start + l1_start_idx + l2_start_idx + l3_end_idx,
                        }
                    )
                    page_global_chunk_idx += 1  # 更新全局 chunk 索引

        # 返回根块列表
        return root_chunks

    def load_document(
        self,
        file_path: str,
        filename: str,
        kb_scope: str,
        user_id: int,
        agent_id: int,
    ) -> list[dict]:
        """
        加载文档，进行三级分块和图片提取
        :param file_path: 文件路径
        :param filename: 文件名
        :param kb_scope: 知识库范围
        :param user_id: 用户ID
        :param agent_id: 智能体ID
        :return: 分块后的文档列表
        """
        # 根据文件名确定文档类型
        file_lower = filename.lower()

        if file_lower.endswith(".pdf"):
            doc_type = "PDF"
            loader = PyPDFLoader(file_path)
            # 从 PDF 中提取图片（带位置信息）
            images = self._extract_images_from_pdf(file_path, user_id, agent_id, kb_scope, filename)
        elif file_lower.endswith((".docx", ".doc")):
            doc_type = "Word"
            loader = Docx2txtLoader(file_path)
            # 从 DOCX 中提取图片
            images = self._extract_images_from_docx(file_path, user_id, agent_id, kb_scope, filename)
        elif file_lower.endswith((".xlsx", ".xls")):
            doc_type = "Excel"
            loader = UnstructuredExcelLoader(file_path)
            images = []  # Excel 暂不支持图片提取
        else:
            raise ValueError(f"不支持的文件类型: {filename}")

        # 加载文档文本
        raw_docs = loader.load()
        documents: list[dict] = []
        page_global_chunk_idx = 0

        # 处理每一页的文本
        for doc in raw_docs:
            # 构建每一页的基础文档信息
            base_doc = {
                "kb_scope": kb_scope,
                "filename": filename,
                "file_path": file_path,
                "file_type": doc_type,
                "page_number": doc.metadata.get("page", 0),
                "user_id": user_id,
                "agent_id": agent_id,
            }
            
            # 获取页面文本的长度（用于计算位置）
            page_text = (doc.page_content or "").strip()
            
            # 进行三级分块
            page_chunks = self._split_page_to_three_levels(
                text=page_text,
                base_doc=base_doc,
                page_global_chunk_idx=page_global_chunk_idx,
                page_text_start=0,  # 每页重新计算位置
            )
            
            # 更新全局 chunk 索引
            page_global_chunk_idx += len(page_chunks)
            # 将分块后的文档添加到结果列表
            documents.extend(page_chunks)
        
        # 建立文本块和图片的关联（PDF 使用与分块相同的 0 起算页码 + 页面坐标系）
        documents, images = self._associate_text_with_images(
            documents, images, file_path=file_path, doc_type=doc_type
        )
        
        # 创建图片块
        if images:
            # 为每张图片创建块
            for img_info in images:
                # 构建图片的基础信息
                image_base_doc = {
                    "kb_scope": kb_scope,
                    "filename": filename,
                    "file_path": file_path,
                    "file_type": doc_type,
                    "page_number": img_info.get("page_number", 0),
                    "user_id": user_id,
                    "agent_id": agent_id,
                }
                
                # 创建图片块
                image_chunks = self._create_image_chunks(
                    [img_info],
                    image_base_doc,
                    page_global_chunk_idx,
                )
                
                # 添加图片元数据（用于后续存储到数据库）
                for chunk in image_chunks:
                    chunk["image_metadata"] = img_info
                
                documents.extend(image_chunks)
                page_global_chunk_idx += len(image_chunks)

        logger.info(f"Loaded {filename}: {len(documents)} chunks (including {len(images)} images)")
        
        # 返回分块后的文档列表
        return documents
