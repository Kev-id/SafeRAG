"""文本切块与编码处理 — 知识库构建的公共逻辑。

从 scripts/build_knowledge_base.py 抽出，供上传接口和重建脚本共用：
    - decode_text / read_text：兼容 utf-8 / gbk 编码
    - split_text：自适应切块（一行一条 vs 成段文章）
"""

import hashlib
import re

# 单个 chunk 的最大字符数（超过则按句末标点二次切分）
MAX_CHUNK_CHARS = 200


def file_md5(text: str) -> str:
    """基于归一化后的文本内容算 md5，用于判断文件是否变化。"""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def decode_text(content: bytes) -> str:
    """把 bytes 解码成文本，自动兼容 utf-8 / gbk / gb18030。"""
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别文件编码")


def read_text(path: str) -> str:
    """读文本文件，自动兼容 utf-8 / gbk / gb18030。"""
    with open(path, "rb") as f:
        return decode_text(f.read())


def _split_long(paragraph: str, max_chars: int) -> list[str]:
    """长段落按句末标点（。！？；）切成不超过 max_chars 的块。"""
    sentences = re.split(r"(?<=[。！？；])", paragraph)
    chunks, buf = [], ""
    for s in sentences:
        if buf and len(buf) + len(s) > max_chars:
            chunks.append(buf.strip())
            buf = s
        else:
            buf += s
    if buf.strip():
        chunks.append(buf.strip())
    return chunks


def _looks_like_line_items(lines: list[str]) -> bool:
    """判断是否"一行一条"格式：绝大多数行以句末标点结尾。"""
    if not lines:
        return False
    ended = sum(1 for l in lines if l.endswith(("。", "！", "？", "；")))
    return ended / len(lines) >= 0.8


def _split_lines(lines: list[str], max_chars: int) -> list[str]:
    """按行切：每行一块，超长行按句末标点二次切。"""
    chunks = []
    for line in lines:
        if len(line) <= max_chars:
            chunks.append(line)
        else:
            chunks.extend(_split_long(line, max_chars))
    return chunks


def _split_paragraphs(text: str, max_chars: int) -> list[str]:
    """按空行分段落，长段按句末标点二次切。"""
    paragraphs = re.split(r"\n\s*\n", text)
    chunks = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            chunks.append(para)
        else:
            chunks.extend(_split_long(para, max_chars))
    return chunks


def split_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """自适应切块：一行一条 → 按行切；成段文章 → 按空行分段落切。

    长块统一按句末标点二次切到 max_chars 以内。
    """
    lines = [l.strip() for l in text.split("\n")]
    lines = [l for l in lines if l]

    if _looks_like_line_items(lines):
        return _split_lines(lines, max_chars)
    return _split_paragraphs(text, max_chars)
