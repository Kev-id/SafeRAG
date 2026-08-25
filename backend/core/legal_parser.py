"""文档解析器 — 文件内容 → 结构化文档树。

把任意文本/文档解析成统一 schema 的文档树，再从树生成 chunks。
这是知识库"解析 → 入库"之间活合同的产出侧：解析段调 parse_to_tree 得到树，
入库段 iter_legal_chunks 只认树。新文件格式（docx/pdf）只需在 _EXTRACTORS
分派表里加一行 bytes→文本 提取器，入库链路零改动。

树落 SQLite kb_trees 表（见 kb_tree_repo）。
"""

from __future__ import annotations

import re

from backend.core.chunker import decode_text, file_md5


_CHAPTER_RE = re.compile(r"^(第[一二三四五六七八九十百千万0-9]+章)\s*(.*)$")
_SECTION_RE = re.compile(r"^(第[一二三四五六七八九十百千万0-9]+节)\s*(.*)$")
_ARTICLE_RE = re.compile(r"^(第[一二三四五六七八九十百千万0-9]+条)\s*(.*)$")
_LEGAL_HEAD_RE = re.compile(r"^(第[一二三四五六七八九十百千万0-9]+[章节条])")

# 全角空格（U+3000）常见于"目　　录""总　　则"，_clean_line 不去它会导致标题/目录探测失效。
_FULLWIDTH_SPACE = "　"


def _clean_line(line: str) -> str:
    """去空白（含全角空格）后再 strip，用于结构头匹配。"""
    return re.sub(rf"[\s{re.escape(_FULLWIDTH_SPACE)}]+", "", line).strip()


def _normalize_text(text: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines()]
    return [line for line in lines if line]


def _is_legal_text(text: str) -> bool:
    """是否够格当法规解析：章/节/条结构头达阈值。"""
    lines = _normalize_text(text)
    heads = sum(1 for line in lines if _LEGAL_HEAD_RE.match(_clean_line(line)))
    return heads >= 3


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？；])", text)
    return [p.strip() for p in parts if p.strip()]


def _split_long_text(text: str, max_chars: int) -> list[str]:
    """把过长条文按句子切开。"""
    chunks: list[str] = []
    buf = ""
    for sent in _split_sentences(text):
        if buf and len(buf) + len(sent) > max_chars:
            chunks.append(buf.strip())
            buf = sent
        else:
            buf += sent
    if buf.strip():
        chunks.append(buf.strip())
    return chunks


def extract_text(content: bytes, filename: str) -> str:
    """按文件名后缀把 bytes 变成文本（规则文档入库的统一文本提取入口）。

    .txt  ：现有 decode_text 自动兼容 utf-8/gbk/gb18030。
    .docx ：python-docx 按段落抽文本（Heading/正文段落都取 text，默认为条文行，
            txt 解析器按"第X章/条第X条"开头自然建树）。
    .pdf  ：pypdf 抽文本层；扫描件无文本层 → 抛 ValueError。
    各格式用延迟 import——txt-only 部署不装 python-docx/pypdf 也能跑，只有真处理
    docx/pdf 时才需要那两个库。
    """
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext == "txt":
        return decode_text(content)
    if ext == "docx":
        return _extract_docx(content)
    if ext == "pdf":
        return _extract_pdf(content)
    raise ValueError(f"不支持的文件后缀: .{ext}")


def _extract_docx(content: bytes) -> str:
    """python-docx 按段落抽文本，每段一行。"""
    import io
    from docx import Document

    doc = Document(io.BytesIO(content))
    lines = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    if not lines:
        raise ValueError("docx 无文本内容")
    return "\n".join(lines)


def _extract_pdf(content: bytes) -> str:
    """pypdf 抽文本层；扫描件/损坏 PDF 明确报错（转 ValueError 让上层统一处理）。"""
    import io
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(content))
        parts = [page.extract_text() or "" for page in reader.pages]
    except PdfReadError as e:
        raise ValueError(f"无法解析 PDF: {e}") from e
    text = "\n".join(p.strip() for p in parts).strip()
    # 合法结构但无文本层（扫描件/图片型）：extract_text 为空 → 明确提示而不是建垃圾树。
    # 阈值 30：短但合法的单页文档（几十字）不应被误杀，法规文档远长于此。
    if len(text) < 30:
        raise ValueError("PDF 无可用文本（可能是扫描件/图片型），暂不支持，请提供文本型 PDF")
    return text


def parse_to_tree(content: bytes, filename: str, file_type: str) -> tuple[dict, str]:
    """把任意文件内容解析成文档树（解析与入库的唯一合同入口）。

    按 filename 后缀取文本提取器（_extract_text），再走向法规层级树 / 非法规最简树。
    返回 (tree, md5)：md5 基于提取出的文本算（语义沿用"解码后文本"），存量 kb_files 无需迁移。

    法规文本（章/节/条头足够）→ 层级文档树；
    其它文本 → 最简文档树（单根 article 节点装整段正文），与法规走同一条入库路径，
    入库层不再需要兜底——所有文本统一先变树，再 iter_legal_chunks 出块。
    """
    text = extract_text(content, filename)
    if not text.strip():
        raise ValueError("空文本")
    md5 = file_md5(text)
    if _is_legal_text(text):
        return _parse_legal(text, source=filename, file_type=file_type), md5
    tree = {
        "doc": {"title": "", "file_type": file_type, "source": filename, "meta": None},
        "toc": [],
        "tree": [{"level": "article", "no": "", "title": "", "text": text.strip()}],
    }
    return tree, md5


def _parse_legal(text: str, source: str, file_type: str) -> dict:
    """把法规 txt 解析成文档树。"""
    lines = _normalize_text(text)
    if not lines:
        raise ValueError("空文本")

    title = lines[0]
    meta = lines[1] if len(lines) > 1 and lines[1].startswith("（") and lines[1].endswith("）") else None

    idx = 1 if meta is None else 2
    toc: list[dict] = []
    seen_chapters: set[str] = set()
    body_start = idx

    while idx < len(lines):
        line = _clean_line(lines[idx])
        # _clean_line 已去全角空格，"目　　录" 会归一成 "目录"
        if line == "目录":
            idx += 1
            body_start = idx
            break
        idx += 1

    while idx < len(lines):
        line = _clean_line(lines[idx])
        chapter_match = _CHAPTER_RE.match(line)
        if chapter_match:
            chapter_no, chapter_title = chapter_match.groups()
            if chapter_no in seen_chapters:
                body_start = idx
                break
            toc.append({"level": "chapter", "no": chapter_no, "title": chapter_title or None})
            seen_chapters.add(chapter_no)
        idx += 1

    chapters: list[dict] = []
    current_chapter: dict | None = None
    current_section: dict | None = None
    current_article: dict | None = None
    article_buf: list[str] = []

    def flush_article() -> None:
        nonlocal current_article, article_buf, current_section, current_chapter
        if current_article is None:
            return
        current_article["text"] = "\n".join(article_buf).strip()
        if current_chapter is None:
            return
        if current_section is not None:
            current_section.setdefault("children", []).append(current_article)
        else:
            current_chapter.setdefault("children", []).append(current_article)
        current_article = None
        article_buf = []

    def flush_section() -> None:
        nonlocal current_section
        flush_article()
        current_section = None

    for raw_line in lines[body_start:]:
        line = _clean_line(raw_line)
        if not line:
            continue

        chapter_match = _CHAPTER_RE.match(line)
        if chapter_match:
            flush_section()
            chapter_no, chapter_title = chapter_match.groups()
            current_chapter = {
                "level": "chapter",
                "no": chapter_no,
                "title": chapter_title or "",
                "children": [],
            }
            chapters.append(current_chapter)
            continue

        section_match = _SECTION_RE.match(line)
        if section_match:
            flush_section()
            section_no, section_title = section_match.groups()
            current_section = {
                "level": "section",
                "no": section_no,
                "title": section_title or "",
                "children": [],
            }
            if current_chapter is None:
                current_chapter = {
                    "level": "chapter",
                    "no": "",
                    "title": "",
                    "children": [],
                }
                chapters.append(current_chapter)
            current_chapter.setdefault("children", []).append(current_section)
            continue

        article_match = _ARTICLE_RE.match(line)
        if article_match:
            flush_article()
            article_no, article_text = article_match.groups()
            if current_chapter is None:
                current_chapter = {
                    "level": "chapter",
                    "no": "",
                    "title": "",
                    "children": [],
                }
                chapters.append(current_chapter)
            current_article = {
                "level": "article",
                "no": article_no,
                "title": "",
                "text": "",
            }
            article_buf = [article_text] if article_text else []
            continue

        if current_article is not None:
            article_buf.append(raw_line)
        elif current_section is not None:
            current_section.setdefault("text", [])
            current_section["text"].append(raw_line)
        elif current_chapter is not None:
            current_chapter.setdefault("text", [])
            current_chapter["text"].append(raw_line)

    flush_article()

    return {
        "doc": {
            "title": title,
            "file_type": file_type,
            "source": source,
            "meta": meta,
        },
        "toc": toc,
        "tree": chapters,
    }


def iter_legal_chunks(tree_data: dict, max_chars: int = 400) -> tuple[list[str], list[dict]]:
    """从文档树生成索引 chunks 和对应 metadata。

    每个条文先按 max_chars 切，超长的会被拆成多个块，用 article_chunk 编号区分。
    返回的 metadata 字段是固定 schema——每块一定带 file_type/article_no 等。
    """
    doc = tree_data["doc"]
    chunks: list[str] = []
    metas: list[dict] = []

    def push_article(chapter_no: str, chapter_title: str,
                     section_no: str, section_title: str,
                     article_no: str, article_text: str) -> None:
        article_text = article_text.strip()
        if not article_text:
            return
        pieces = _split_long_text(article_text, max_chars)
        prefix_parts = [doc["title"], chapter_no, chapter_title]
        if section_no:
            prefix_parts += [section_no, section_title]
        for i, piece in enumerate(pieces, 1):
            prefix = " ".join(p for p in prefix_parts if p)
            chunks.append(f"{prefix}\n{article_no} {piece}".strip())
            meta = {
                "source": doc["source"],
                "doc_title": doc["title"],
                "file_type": doc["file_type"],
                "chapter_no": chapter_no,
                "chapter_title": chapter_title,
                "article_no": article_no,
                "article_chunk": i,
            }
            if section_no:
                meta["section_no"] = section_no
                meta["section_title"] = section_title
            metas.append(meta)

    for node in tree_data.get("tree", []):
        # tree 顶层节点既可以是章节，也可以是裸条文（非法规最简树就是单个裸 article）。
        if node.get("level") == "article":
            push_article("", "", "", "", node.get("no", ""), node.get("text", ""))
            continue
        chapter_no = node.get("no", "")
        chapter_title = node.get("title", "")
        for child in node.get("children", []):
            if child.get("level") == "section":
                for article in child.get("children", []):
                    push_article(
                        chapter_no, chapter_title,
                        child.get("no", ""), child.get("title", ""),
                        article.get("no", ""), article.get("text", ""),
                    )
            else:
                push_article(
                    chapter_no, chapter_title,
                    "", "",
                    child.get("no", ""), child.get("text", ""),
                )

    return chunks, metas
