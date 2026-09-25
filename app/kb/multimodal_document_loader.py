"""
多模态文档加载与结构感知分块，支持图片提取和存储。

- Markdown / 纯文本 / 源码 / CSV：结构感知分块（代码块与表格原子化，源码走 tree-sitter AST）
- PDF / Word：正文沿用递归字符切分，表格单独抽出为 table 块
- Excel：逐 sheet 转 Markdown 表格块
图片作为独立的 L4 块处理，与文本块通过位置关联。
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF
from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from loguru import logger

from app.kb.multimodal_embedding import get_multimodal_embedding_service
from app.kb.structural_chunker import (
    BLOCK_TEXT,
    PARENT_L1_MAX_CHARS,
    PARENT_L2_MAX_CHARS,
    Leaf,
    build_embed_text,
    cap_text,
    make_table_leaves,
    parse_csv_rows,
    segment_document,
    segment_plain_text,
)
from app.settings import settings
from app.utils.document_types import doc_kind


def _filename_fingerprint(filename: str) -> str:
    """
    使用 SHA256 构建文件名指纹，用于构建 chunk_id
    :param filename: 文件名
    :return: 文件名指纹
    """
    return hashlib.sha256(filename.encode("utf-8")).hexdigest()[:16]


def _chunk_limits() -> tuple[int, int, int, bool]:
    """结构感知分块参数：字符上限 / 字节上限 / 表格行上限 / 是否启用代码专用切分。"""
    return (
        max(64, int(getattr(settings, "KB_ATOMIC_BLOCK_MAX_CHARS", 1200) or 1200)),
        max(64, int(getattr(settings, "KB_ATOMIC_BLOCK_MAX_BYTES", 1800) or 1800)),
        max(1, int(getattr(settings, "KB_TABLE_MAX_ROWS_PER_CHUNK", 200) or 200)),
        bool(getattr(settings, "KB_CODE_CHUNKING_ENABLED", True)),
    )


def _read_text_file(file_path: str) -> str:
    """文本/源码文件解码：BOM 去除，UTF-8 优先，charset-normalizer / GB18030 兜底。"""
    raw = Path(file_path).read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(raw).best()
        if best is not None:
            return str(best)
    except Exception:
        pass
    for enc in ("gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


class MultimodalDocumentLoader:
    def __init__(self, chunk_size: int = 900, chunk_overlap: int = 90) -> None:
        """
        初始化多模态分块器
        :param chunk_size: 散文叶子目标大小（默认 900，取一半 450 作为叶子块）
        :param chunk_overlap: 分块重叠大小
        """
        self._prose_leaf_size = max(300, chunk_size // 2)
        self._prose_leaf_overlap = max(60, chunk_overlap // 2)
        self._splitter_prose = RecursiveCharacterTextSplitter(
            chunk_size=self._prose_leaf_size,
            chunk_overlap=self._prose_leaf_overlap,
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
        images_root_dir: str,
    ) -> List[Dict[str, Any]]:
        """
        从 PDF 中提取真实的图片对象（使用 PyMuPDF），并获取图片位置信息
        :param pdf_path: PDF 文件路径
        :param user_id: 用户ID
        :param agent_id: 智能体ID
        :param kb_scope: 知识库范围
        :param filename: 文件名
        :param images_root_dir: 图片输出根目录（调用方提供的临时目录；子结构 user_{uid}/{aid}/{fingerprint}/ 与对象 key 一致）
        :return: 图片信息列表
        """
        images = []
        try:
            # 创建图片输出目录（临时目录内保持与对象 key 相同的相对结构）
            images_dir = Path(images_root_dir) / f"user_{user_id}" / str(agent_id) / _filename_fingerprint(filename)
            images_dir.mkdir(parents=True, exist_ok=True)
            
            # 使用 PyMuPDF 打开 PDF
            doc = fitz.open(pdf_path)
            
            # 遍历每一页
            for page_num in range(len(doc)):
                page = doc[page_num]

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
                        
                        # 生成图片文件名：带内容哈希后缀，避免同名文档替换上传时新旧图片 key 冲突
                        # （否则临界区删除旧图会误删同 key 的新图）
                        content_tag = hashlib.sha256(image_bytes).hexdigest()[:8]
                        image_filename = f"page_{page_num + 1:04d}_img_{img_index + 1:04d}_{content_tag}.{image_ext}"
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
                            "size_bytes": len(image_bytes),
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
        images_root_dir: str,
    ) -> List[Dict[str, Any]]:
        """
        从 DOCX 中提取图片
        :param docx_path: DOCX 文件路径
        :param user_id: 用户ID
        :param agent_id: 智能体ID
        :param kb_scope: 知识库范围
        :param filename: 文件名
        :param images_root_dir: 图片输出根目录（调用方提供的临时目录；子结构与对象 key 一致）
        :return: 图片信息列表
        """
        images = []
        try:
            import docx
            from PIL import Image

            # 创建图片输出目录（临时目录内保持与对象 key 相同的相对结构）
            images_dir = Path(images_root_dir) / f"user_{user_id}" / str(agent_id) / _filename_fingerprint(filename)
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
                            # 保存图片（文件名带内容哈希后缀，避免同名文档替换上传时新旧图片 key 冲突）
                            content_tag = hashlib.sha256(image_data).hexdigest()[:8]
                            image_path = images_dir / f"para_{para_idx:04d}_run_{run_idx:04d}_img_{image_idx:04d}_{content_tag}.png"
                            with open(image_path, 'wb') as f:
                                f.write(image_data)
                            
                            # 获取图片信息
                            try:
                                with Image.open(image_path) as img:
                                    width, height = img.size
                                    img_format = img.format.lower() if img.format else "png"
                            except Exception:
                                width, height = 0, 0
                                img_format = "png"
                            
                            # 构建图片信息
                            image_info = {
                                "kb_scope": kb_scope,
                                "filename": filename,
                                "file_type": "Word",
                                "page_number": para_idx + 1,  # 使用段落索引作为页码
                                "stored_path": str(image_path),
                                "size_bytes": len(image_data),
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

    def _prose_page_leaves(
        self,
        text: str,
        filename: str,
        parent_titles: Tuple[str, ...],
        *,
        page_text_start: int = 0,
    ) -> List[Leaf]:
        """
        散文文本 -> L3 叶子（沿用递归字符切分，作为结构树的普通文本块）。
        :param parent_titles: 父级标题路径（L1/L2），用于层级构建与嵌入上下文
        """
        if not text or not text.strip():
            return []
        leaves: List[Leaf] = []
        for doc in self._splitter_prose.create_documents([text], [{}]):
            piece = (doc.page_content or "").strip()
            if not piece:
                continue
            start = int(doc.metadata.get("start_index", 0) or 0) + page_text_start
            end = start + len(piece)
            leaves.append(
                Leaf(
                    text=piece,
                    block_type=BLOCK_TEXT,
                    parent_titles=parent_titles or (filename,),
                    heading_path=tuple(t for t in parent_titles if t) or (filename,),
                    start=start,
                    end=end,
                    embed_text=build_embed_text(filename, piece, heading_path=parent_titles),
                )
            )
        return leaves

    def _build_hierarchy(
        self,
        leaves: List[Leaf],
        base_doc: Dict[str, Any],
        page_global_chunk_idx: int,
    ) -> List[Dict[str, Any]]:
        """
        将叶子按 parent_titles 组织为 L1/L2/L3 块（L1/L2 文本为子块正文拼接并截断）。
        叶子计数器在本页内唯一，避免 text/code/table 混排时 chunk_id 冲突。
        """
        if not leaves:
            return []
        kb_scope = base_doc["kb_scope"]
        filename = base_doc["filename"]
        page_number = int(base_doc.get("page_number", 0) or 0)

        l1_order: List[str] = []
        l1_children: Dict[str, List[Leaf]] = defaultdict(list)
        l2_order: List[Tuple[str, str]] = []
        l2_children: Dict[Tuple[str, str], List[Leaf]] = defaultdict(list)

        def titles_of(leaf: Leaf) -> Tuple[str, ...]:
            titles = tuple(t for t in (leaf.parent_titles or ()) if t)
            return titles or (filename or "文档",)

        for leaf in leaves:
            titles = titles_of(leaf)
            l1 = titles[0]
            l2 = titles[1] if len(titles) > 1 else ""
            if l1 not in l1_children:
                l1_order.append(l1)
            l1_children[l1].append(leaf)
            if l2:
                key = (l1, l2)
                if key not in l2_children:
                    l2_order.append(key)
                l2_children[key].append(leaf)

        chunks: List[Dict[str, Any]] = []
        l1_ids: Dict[str, str] = {}
        for l1 in l1_order:
            cid = self._build_chunk_id(kb_scope, filename, page_number, 1, len(l1_ids))
            body = cap_text("\n\n".join(leaf.text for leaf in l1_children[l1]), PARENT_L1_MAX_CHARS)
            l1_ids[l1] = cid
            chunks.append(
                {
                    **base_doc,
                    "text": body,
                    "content_type": "text",
                    "block_type": BLOCK_TEXT,
                    "code_language": "",
                    "chunk_id": cid,
                    "parent_chunk_id": "",
                    "root_chunk_id": cid,
                    "chunk_level": 1,
                    "chunk_idx": page_global_chunk_idx,
                    "position_start": 0,
                    "position_end": 0,
                }
            )
            page_global_chunk_idx += 1

        l2_ids: Dict[Tuple[str, str], str] = {}
        for key in l2_order:
            l1 = key[0]
            cid = self._build_chunk_id(kb_scope, filename, page_number, 2, len(l2_ids))
            body = cap_text(
                "\n\n".join(leaf.text for leaf in l2_children[key]), PARENT_L2_MAX_CHARS
            )
            l2_ids[key] = cid
            chunks.append(
                {
                    **base_doc,
                    "text": body,
                    "content_type": "text",
                    "block_type": BLOCK_TEXT,
                    "code_language": "",
                    "chunk_id": cid,
                    "parent_chunk_id": l1_ids[l1],
                    "root_chunk_id": l1_ids[l1],
                    "chunk_level": 2,
                    "chunk_idx": page_global_chunk_idx,
                    "position_start": 0,
                    "position_end": 0,
                }
            )
            page_global_chunk_idx += 1

        for leaf_idx, leaf in enumerate(leaves):
            titles = titles_of(leaf)
            l1 = titles[0]
            l2 = titles[1] if len(titles) > 1 else ""
            parent_id = l2_ids.get((l1, l2)) or l1_ids.get(l1, "")
            cid = self._build_chunk_id(kb_scope, filename, page_number, 3, leaf_idx)
            chunks.append(
                {
                    **base_doc,
                    "text": leaf.text,
                    "embed_text": leaf.embed_text
                    or build_embed_text(
                        filename,
                        leaf.text,
                        block_type=leaf.block_type,
                        language=leaf.language,
                        heading_path=leaf.heading_path,
                    ),
                    "content_type": "text",
                    "block_type": leaf.block_type or BLOCK_TEXT,
                    "code_language": leaf.language or "",
                    "chunk_id": cid,
                    "parent_chunk_id": parent_id,
                    "root_chunk_id": l1_ids.get(l1, cid),
                    "chunk_level": 3,
                    "chunk_idx": page_global_chunk_idx,
                    "position_start": leaf.start,
                    "position_end": leaf.end,
                }
            )
            page_global_chunk_idx += 1

        return chunks

    @staticmethod
    def _excel_sheets(file_path: str) -> List[Tuple[str, List[List[str]]]]:
        """逐 sheet 读取为二维单元格（隐藏/空 sheet 跳过）。"""
        from openpyxl import load_workbook

        workbook = load_workbook(file_path, read_only=True, data_only=True)
        sheets: List[Tuple[str, List[List[str]]]] = []
        try:
            for sheet in workbook.worksheets:
                if sheet.sheet_state == "hidden":
                    continue
                rows: List[List[str]] = []
                for row in sheet.iter_rows(values_only=True):
                    cells = ["" if v is None else str(v) for v in row]
                    while cells and cells[-1] == "":
                        cells.pop()
                    rows.append(cells)
                while rows and not any(c.strip() for c in rows[0]):
                    rows.pop(0)
                while rows and not any(c.strip() for c in rows[-1]):
                    rows.pop()
                if rows:
                    sheets.append((sheet.title, rows))
        finally:
            workbook.close()
        return sheets

    @staticmethod
    def _pdf_tables_by_page(file_path: str) -> Dict[int, List[List[List[str]]]]:
        """PDF 逐页表格提取（PyMuPDF find_tables）；页 -> 表格列表 -> 行列表。"""
        min_rows = max(1, int(getattr(settings, "KB_PDF_TABLE_MIN_ROWS", 2) or 2))
        min_cols = max(1, int(getattr(settings, "KB_PDF_TABLE_MIN_COLS", 2) or 2))
        result: Dict[int, List[List[List[str]]]] = {}
        try:
            doc = fitz.open(file_path)
        except Exception as e:
            logger.warning("PDF 表格提取失败，跳过: {}", e)
            return result
        try:
            for page_idx in range(doc.page_count):
                try:
                    tables = doc[page_idx].find_tables()
                except Exception:
                    continue
                page_tables: List[List[List[str]]] = []
                for table in getattr(tables, "tables", []) or []:
                    try:
                        raw_rows = table.extract() or []
                    except Exception:
                        continue
                    rows = [["" if c is None else str(c) for c in r] for r in raw_rows if r]
                    rows = [r for r in rows if any(c.strip() for c in r)]
                    if len(rows) < min_rows:
                        continue
                    if max((len(r) for r in rows), default=0) < min_cols:
                        continue
                    page_tables.append(rows)
                if page_tables:
                    result[page_idx] = page_tables
        finally:
            doc.close()
        return result

    @staticmethod
    def _word_blocks(file_path: str) -> List[Tuple[str, Any, int]]:
        """Word 正文按阅读顺序展开为 (类型, 内容, 段落序号)：para / table。"""
        import docx
        from docx.table import Table as DocxTable
        from docx.text.paragraph import Paragraph as DocxParagraph

        document = docx.Document(file_path)
        blocks: List[Tuple[str, Any, int]] = []
        para_idx = 0
        for child in document.element.body.iterchildren():
            tag = str(child.tag).rsplit("}", 1)[-1]
            if tag == "p":
                paragraph = DocxParagraph(child, document)
                text = (paragraph.text or "").strip()
                if text:
                    blocks.append(("para", text, para_idx))
                para_idx += 1
            elif tag == "tbl":
                table = DocxTable(child, document)
                rows = [[(cell.text or "").strip() for cell in row.cells] for row in table.rows]
                rows = [r for r in rows if any(c for c in r)]
                if rows:
                    blocks.append(("table", rows, para_idx))
        return blocks

    def _word_leaves(
        self,
        blocks: List[Tuple[str, Any, int]],
        filename: str,
        *,
        max_chars: int,
        max_bytes: int,
        max_table_rows: int,
    ) -> List[Leaf]:
        """Word blocks -> 叶子：连续段落聚合为散文，表格独立成 table 叶子。"""
        leaves: List[Leaf] = []
        buf: List[str] = []
        table_no = 0

        def flush() -> None:
            nonlocal buf
            if buf:
                leaves.extend(self._prose_page_leaves("\n".join(buf), filename, (filename,)))
                buf = []

        for kind, content, _para_idx in blocks:
            if kind == "para":
                buf.append(content)
                continue
            flush()
            table_no += 1
            leaves.extend(
                make_table_leaves(
                    content,
                    filename=filename,
                    title=f"表格 {table_no}",
                    max_chars=max_chars,
                    max_bytes=max_bytes,
                    max_table_rows=max_table_rows,
                )
            )
        flush()
        return leaves

    def load_document(
        self,
        file_path: str,
        filename: str,
        kb_scope: str,
        user_id: int,
        agent_id: int,
        images_root_dir: str,
    ) -> list[dict]:
        """
        加载文档：结构感知分块（代码块/表格/源码 AST）+ 图片提取。
        :param file_path: 文件路径（调用方负责的本地文件，通常为临时文件）
        :param filename: 文件名
        :param kb_scope: 知识库范围
        :param user_id: 用户ID
        :param agent_id: 智能体ID
        :param images_root_dir: 图片输出根目录（调用方提供的临时目录；子结构与对象 key 一致）
        :return: 分块后的文档列表（L1/L2/L3 文本与 L4 图片）
        """
        kind = doc_kind(filename)
        max_chars, max_bytes, max_table_rows, code_enabled = _chunk_limits()
        structural = bool(getattr(settings, "KB_STRUCTURAL_CHUNKING_ENABLED", True))
        pdf_tables_enabled = bool(getattr(settings, "KB_PDF_TABLE_EXTRACTION", True))

        common: Dict[str, Any] = {
            "kb_scope": kb_scope,
            "filename": filename,
            "file_path": file_path,
            "user_id": user_id,
            "agent_id": agent_id,
        }
        documents: list[dict] = []
        images: List[Dict[str, Any]] = []
        idx = 0
        doc_type = "Text"

        if kind == "pdf":
            doc_type = "PDF"
            raw_docs = PyPDFLoader(file_path).load()
            images = self._extract_images_from_pdf(
                file_path, user_id, agent_id, kb_scope, filename, images_root_dir
            )
            tables_by_page = (
                self._pdf_tables_by_page(file_path) if (pdf_tables_enabled and structural) else {}
            )
            for doc in raw_docs:
                page = int(doc.metadata.get("page", 0) or 0)
                base = {**common, "file_type": doc_type, "page_number": page}
                leaves = self._prose_page_leaves(
                    (doc.page_content or "").strip(), filename, (filename, f"第{page + 1}页")
                )
                chunks = self._build_hierarchy(leaves, base, idx)
                documents.extend(chunks)
                idx += len(chunks)
                for table_rows in tables_by_page.get(page, []):
                    table_leaves = make_table_leaves(
                        table_rows,
                        filename=filename,
                        title=f"第{page + 1}页 表格",
                        max_chars=max_chars,
                        max_bytes=max_bytes,
                        max_table_rows=max_table_rows,
                    )
                    table_chunks = self._build_hierarchy(table_leaves, base, idx)
                    documents.extend(table_chunks)
                    idx += len(table_chunks)

        elif kind == "word":
            doc_type = "Word"
            images = self._extract_images_from_docx(
                file_path, user_id, agent_id, kb_scope, filename, images_root_dir
            )
            blocks = self._word_blocks(file_path)
            base = {**common, "file_type": doc_type, "page_number": 0}
            if structural:
                leaves = self._word_leaves(
                    blocks,
                    filename,
                    max_chars=max_chars,
                    max_bytes=max_bytes,
                    max_table_rows=max_table_rows,
                )
            else:
                prose = "\n".join(content for k, content, _ in blocks if k == "para")
                leaves = self._prose_page_leaves(prose, filename, (filename,))
            chunks = self._build_hierarchy(leaves, base, idx)
            documents.extend(chunks)
            idx += len(chunks)

        elif kind == "excel":
            doc_type = "Excel"
            for sheet_index, (title, rows) in enumerate(self._excel_sheets(file_path), 1):
                base = {**common, "file_type": doc_type, "page_number": sheet_index}
                leaves = make_table_leaves(
                    rows,
                    filename=filename,
                    title=f"工作表：{title}",
                    max_chars=max_chars,
                    max_bytes=max_bytes,
                    max_table_rows=max_table_rows,
                )
                chunks = self._build_hierarchy(leaves, base, idx)
                documents.extend(chunks)
                idx += len(chunks)

        elif kind in ("markdown", "text", "csv", "code"):
            doc_type = "Code" if kind == "code" else "Text"
            text = _read_text_file(file_path)
            base = {**common, "file_type": doc_type, "page_number": 0}
            if kind == "csv":
                leaves = make_table_leaves(
                    parse_csv_rows(text),
                    filename=filename,
                    max_chars=max_chars,
                    max_bytes=max_bytes,
                    max_table_rows=max_table_rows,
                )
            elif structural:
                leaves = segment_document(
                    filename,
                    text,
                    force_kind=kind,
                    max_chars=max_chars,
                    max_bytes=max_bytes,
                    max_table_rows=max_table_rows,
                    code_enabled=code_enabled,
                )
            else:
                leaves = segment_plain_text(text, filename, max_chars=max_chars, max_bytes=max_bytes)
            chunks = self._build_hierarchy(leaves, base, idx)
            documents.extend(chunks)
            idx += len(chunks)

        else:
            raise ValueError(f"不支持的文件类型: {filename}")

        # 建立文本块和图片的关联（PDF 使用与分块相同的 0 起算页码 + 页面坐标系）
        documents, images = self._associate_text_with_images(
            documents, images, file_path=file_path, doc_type=doc_type
        )

        # 创建图片块
        if images:
            for img_info in images:
                image_base_doc = {
                    **common,
                    "file_type": doc_type,
                    "page_number": img_info.get("page_number", 0),
                }
                image_chunks = self._create_image_chunks([img_info], image_base_doc, idx)
                for chunk in image_chunks:
                    chunk["image_metadata"] = img_info
                documents.extend(image_chunks)
                idx += len(image_chunks)

        logger.info(f"Loaded {filename}: {len(documents)} chunks (including {len(images)} images)")
        return documents
