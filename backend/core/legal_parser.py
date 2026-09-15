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

# --- 非法规文本的标题探测 ---
# 按"结构感"强弱顺序匹配：多级阿拉伯 > 章/节 > 中文序号 > （中文序号） > 阿拉伯序号 > markdown。
# 检测顺序很重要："1.1.2" 必须先进 _AR_LEVEL_RE，否则会被 _AR_DOT_RE 吃掉前缀。
_AR_LEVEL_RE = re.compile(r"^(\d+(?:\.\d+)+)\s*(.*)$")                    # 1.1 / 1.1.2
_CN_HEAD_RE = re.compile(r"^([一二三四五六七八九十百千万]+)[、．.]\s*(.*)$")  # 一、/一.
_CN_PAREN_RE = re.compile(r"^[（(]([一二三四五六七八九十百千万]+)[)）]\s*(.*)$")  # （一）/(一)
_AR_DOT_RE = re.compile(r"^(\d+)[、．.]\s*(.*)$")                         # 1、/1.
_MD_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")                           # ## xxx（须在原始行上匹配）

_HEAD_MAX_LEN = 50           # 标题行长度上限：正文长句不算标题
_SENT_TERMINAL = "。！？；"   # 完句句读：以这些结尾的行不算标题

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
    """把过长条文按句子切开。

    单个"句子"就超过 max_chars（非标准格式常见的整段粘连、无句读符）时，
    按字符硬切成 ≤max_chars 的小块——否则这条句子会整个成为一个 chunk，
    动辄上千 token，直接把 embedding（位置上限 512）打爆（参见 embedding_client）。
    """
    chunks: list[str] = []
    buf = ""
    for sent in _split_sentences(text):
        if len(sent) > max_chars:
            # 超长句：先把之前的 buf 落盘，再按字符切成若干 ≤max_chars 的块
            if buf.strip():
                chunks.append(buf.strip())
                buf = ""
            for i in range(0, len(sent), max_chars):
                piece = sent[i:i + max_chars].strip()
                if piece:
                    chunks.append(piece)
            continue
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


def parse_to_tree(content: bytes, filename: str, file_type: str, region: str,
                  city: str = "") -> tuple[dict, str]:
    """把任意文件内容解析成文档树（解析与入库的唯一合同入口）。

    按 filename 后缀取文本提取器（_extract_text），再走向法规层级树 / 非法规最简树。
    返回 (tree, md5)：md5 基于提取出的文本算（语义沿用"解码后文本"），存量 kb_files 无需迁移。

    region = 省/直辖市/自治区（空=全国性法规）；city = 地级市（空=省级条例活在 city 粒度下为空）。

    法规文本（章/节/条头足够）→ 层级文档树；
    其它文本 → 分节文档树（有结构标题 → chapter/section/article 层级；无结构 → 按段分块），
    与法规走同一条入库路径——所有文本统一先变树，再 iter_legal_chunks 出块。
    """
    text = extract_text(content, filename)#把文件内容按后缀提取成文本
    if not text.strip():
        raise ValueError("空文本")
    md5 = file_md5(text)
    if _is_legal_text(text):
        return _parse_legal(text, source=filename, file_type=file_type,
                            region=region, city=city), md5
    return _parse_plain(text, source=filename, file_type=file_type,
                        region=region, city=city), md5


def _parse_legal(text: str, source: str, file_type: str, region: str,
                 city: str = "") -> dict:
    """把法规 txt 解析成文档树。"""
    lines = _normalize_text(text) #把文本按行分割，去掉空行和首尾空白
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
            "region": region,
            "city": city,
            "source": source,
            "meta": meta,
        },
        "toc": toc,
        "tree": chapters,
    }


def _heading_pattern(raw_line: str) -> tuple[str, str, int] | None:
    """纯模式探测：一行是否长得像结构标题（不看作上下文），返回 (no, title, level)。

    护栏：清洗后长度 ≤ _HEAD_MAX_LEN、不以句读（。！？；）结尾——完句与超长行都算正文。
    markdown 必须在原始行上匹配（_clean_line 会吞掉 # 后的空格）。
    """
    cleaned = _clean_line(raw_line)
    if not cleaned or len(cleaned) > _HEAD_MAX_LEN:
        return None

    # markdown：必须在原始行上匹配（_clean_line 会把 # 后的空格吞掉）
    md = _MD_HEAD_RE.match(raw_line.strip())
    if md and not cleaned.endswith(tuple(_SENT_TERMINAL)):
        title = md.group(2).strip()
        if title and len(title) <= _HEAD_MAX_LEN:
            return "", title, 1 if len(md.group(1)) == 1 else 2
    if cleaned[-1] in _SENT_TERMINAL:
        return None

    # 顺序敏感：多级阿拉伯优先，防 "1.1.2" 被 _AR_DOT_RE 误吃掉前缀
    m = _AR_LEVEL_RE.match(cleaned)
    if m:
        return m.group(1), m.group(2), min(m.group(1).count(".") + 1, 2)
    m = _CHAPTER_RE.match(cleaned)
    if m:
        return m.group(1), m.group(2), 1
    m = _SECTION_RE.match(cleaned)
    if m:
        return m.group(1), m.group(2), 2
    m = _CN_HEAD_RE.match(cleaned)
    if m:
        return m.group(1) + "、", m.group(2), 1
    m = _CN_PAREN_RE.match(cleaned)
    if m:
        return "（" + m.group(1) + "）", m.group(2), 2
    m = _AR_DOT_RE.match(cleaned)
    if m:
        return m.group(1), m.group(2), 1
    return None


def _heading_of(raw_line: str, next_line: str | None = None) -> tuple[str, str, int] | None:
    """探测非法规文本一行是否够格当结构标题（模式探测 + 上下文护栏）。

    额外护栏（防连续编号列表项被连环当成空章节）：
      - 下一非空行是同一级或更浅级的标题（如 1. 紧跟 2.）→ 这是列表项，退回正文；
      - 下一非空行是更深的标题（如 一、 紧跟 （一））→ 父子关系，仍当标题；
      - 本行是末行（无后继）→ 不当标题，避免产出空章节。
    """
    h = _heading_pattern(raw_line)
    if h is None:
        return None
    if next_line is None:
        return None
    nh = _heading_pattern(next_line)
    if nh is not None and nh[2] <= h[2]:
        return None
    return h


def _parse_plain(text: str, source: str, file_type: str, region: str,
                 city: str = "", max_chars: int = 400) -> dict:
    """非法规文本 → 分节文档树（取代旧的"整段塞单 article"）。

    有结构标题（中文/阿拉伯序号、1.1 多级、markdown、第X章/节）→ 建成
    chapter/section/article 层级树，chunk 带章节元数据，检索更细；
    无结构 → 按空行/长度把正文拆成若干 article，不再一把整块怼进 embedding
    （整段 > 400 字会打爆 BGE 位置上限，参见 embedding_client 注释）。

    入库仍只认树：iter_legal_chunks 对顶层裸 article 与 chapter/section/article
    两种形态都已兼容，本函数只改解析侧，入库/检索链路零改动。
    """
    lines = [ln for ln in text.splitlines()]
    # 掐头去尾空行，保留内部空行做段落边界（_normalize_text 会丢，这里不能复用）
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    preamble: list[dict] = []          # 首个标题之前的正文（顶层裸 article）
    chapters: list[dict] = []
    toc: list[dict] = []
    current_chapter: dict | None = None
    current_section: dict | None = None
    current_article: list[str] | None = None
    art_len = 0
    any_body = False

    def flush_article() -> None:
        nonlocal current_article, art_len, any_body
        if current_article is None:
            return
        body = "\n".join(current_article).strip()
        if body:
            node = {"level": "article", "no": "", "title": "", "text": body}
            if current_section is not None:
                current_section.setdefault("children", []).append(node)
            elif current_chapter is not None:
                current_chapter.setdefault("children", []).append(node)
            else:
                preamble.append(node)
            any_body = True
        current_article = None
        art_len = 0

    def flush_section() -> None:
        nonlocal current_section
        flush_article()
        current_section = None

    def open_chapter(no: str, title: str) -> None:
        nonlocal current_chapter, current_section
        flush_section()
        current_chapter = {"level": "chapter", "no": no, "title": title, "children": []}
        chapters.append(current_chapter)
        toc.append({"level": "chapter", "no": no, "title": title})

    n = len(lines)
    i = 0
    while i < n:
        raw = lines[i]
        if not raw.strip():
            flush_article()      # 空行 = 段落边界
            i += 1
            continue
        nxt: str | None = None
        for j in range(i + 1, n):
            if lines[j].strip():
                nxt = lines[j]
                break
        h = _heading_of(raw, nxt)
        if h is not None:
            no, title, level = h
            flush_article()
            if level <= 1:
                open_chapter(no, title)
            else:
                if current_chapter is None:
                    open_chapter("", "")     # 无一级标题时的匿名章容器
                flush_section()
                current_section = {
                    "level": "section", "no": no, "title": title, "children": [],
                }
                current_chapter.setdefault("children", []).append(current_section)
            i += 1
            continue
        if current_article is None:
            current_article = [raw]
            art_len = len(raw)
        elif art_len + len(raw) > max_chars:
            flush_article()      # 一段无分隔的超长正文也收口，保证块有界
            current_article = [raw]
            art_len = len(raw)
        else:
            current_article.append(raw)
            art_len += len(raw)
        i += 1

    flush_article()

    if not any_body:
        # 全文件都是标题/空行：退回整段单 article 最简树，防止空树 → 空库
        tree: list[dict] = [{"level": "article", "no": "", "title": "", "text": text.strip()}]
        toc = []
    else:
        tree = [*preamble, *chapters]

    return {
        "doc": {"title": "", "file_type": file_type, "region": region,
                "city": city, "source": source, "meta": None},
        "toc": toc,
        "tree": tree,
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
        # region + city 拼进 chunk 文本前缀：让 embedding 向量 / BM25 都"认识"本文的地域属性，
        # 否则地区只存 metadata、不进向量化，检索时无法按地域匹配。
        prefix_parts = []
        if doc.get("region"):
            prefix_parts.append(doc["region"])
        if doc.get("city"):
            prefix_parts.append(doc["city"])
        prefix_parts += [doc["title"], chapter_no, chapter_title]
        if section_no:
            prefix_parts += [section_no, section_title]
        for i, piece in enumerate(pieces, 1):
            prefix = " ".join(p for p in prefix_parts if p)
            chunks.append(f"{prefix}\n{article_no} {piece}".strip())
            meta = {
                "source": doc["source"],
                "doc_title": doc["title"],
                "file_type": doc["file_type"],
                "region": doc["region"],
                "city": doc["city"],
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
