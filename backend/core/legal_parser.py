"""法规/标准 TXT 解析器。

把规整的 txt 法规文本解析成结构化文档树，再从树生成 chunks。
这是知识库的上游真相源之一，避免切块阶段再去猜章/节/条。
"""

from __future__ import annotations

import json
import re
from pathlib import Path


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


def looks_like_legal_txt(text: str) -> bool:
    """粗判是否是法规/标准类文本。"""
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


def parse_legal_txt(text: str, source: str, file_type: str) -> dict:
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
    返回的 metadata 字段是固定 schema——法规块一定带 file_type/article_no 等，
    保证和"非法规块（纯 split_text）"走同一套字段，便于 retriever 统一过滤。
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

    for chapter in tree_data.get("tree", []):
        chapter_no = chapter.get("no", "")
        chapter_title = chapter.get("title", "")
        for node in chapter.get("children", []):
            if node.get("level") == "section":
                for article in node.get("children", []):
                    push_article(
                        chapter_no, chapter_title,
                        node.get("no", ""), node.get("title", ""),
                        article.get("no", ""), article.get("text", ""),
                    )
            else:
                push_article(
                    chapter_no, chapter_title,
                    "", "",
                    node.get("no", ""), node.get("text", ""),
                )

    return chunks, metas


def dump_tree_json(tree_data: dict, path: str) -> None:
    """把树保存为 JSON 侧车文件。"""
    Path(path).write_text(
        json.dumps(tree_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def build_chunks(
    text: str,
    source: str,
    file_type: str,
    tree_path: str | None = None,
) -> tuple[list[str], list[dict] | None, dict | None]:
    """从原始文本统一产出 (chunks, metadatas, tree_data)——service 和 build 脚本的唯一入口。

    法规文本 → 解析成文档树 → 带 metadata 的结构化切块，并把树落盘成 .tree.json 侧车；
    非法规文本 → 退化成 split_text 纯文本切块，metadatas=None（由 kb_store 补默认态）。

    这样"判法规 / 解析 / 切块 / 落树"四个步骤只有一处实现，两处调用不会漂移。
    tree_data=None 表示未走结构化路径（调用方据此决定是否管理侧车文件）。
    """
    if looks_like_legal_txt(text):
        tree_data = parse_legal_txt(text, source=source, file_type=file_type)
        if tree_path is not None:
            dump_tree_json(tree_data, tree_path)
        chunks, metadatas = iter_legal_chunks(tree_data)
        return chunks, metadatas, tree_data

    chunks = _fallback_split(text)
    return chunks, None, None


def _fallback_split(text: str) -> list[str]:
    """非法规文本的兜底切块。延迟 import 避免与 chunker 循环依赖。"""
    from backend.core.chunker import split_text
    return split_text(text)
