"""legal_parser + kb_tree_repo 的单元测试。

覆盖五件事：
1. 法规 txt（标题/说明/目录/章/条）→ 层级文档树，全角空格目录探测正确。
2. 非法规文本 → 分节文档树（有结构标题 → 层级 chapter/section/article；无结构 → 按段分块），
   与法规走同一条入库路径。
3. 文档树经 kb_tree_repo 落盘 SQLite 再读回，与切块忠实——活合同成立。
4. 分段 _split_long_text：多句边界切分、超长单句硬切（保护 embedding 512 token 上限）、无损拼接。
5. 分节护栏：完句长句/超长行不被误判为结构标题。

parse_to_tree 吃 bytes（按后缀分派），故测试输入统一 encode。
"""

import os
import tempfile

# 必须在 import database 之前：把测试库指向临时文件，隔离生产 saferag.db
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP_DB.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.name}"

from backend.core.database import init_db  # noqa: E402
from backend.core.legal_parser import (  # noqa: E402
    _split_long_text,
    iter_legal_chunks,
    parse_to_tree,
)
from backend.repositories import kb_tree_repo  # noqa: E402


def _legal_text():
    return """中华人民共和国防震减灾法
（1997年12月29日第八届全国人民代表大会常务委员会第二十九次会议通过　2008年12月27日第十一届全国人民代表大会常务委员会第六次会议修订）
目　　录
第一章　总　　则
第二章　防震减灾规划
第三章　地震监测预报
第一章　总　　则
第一条　为了防御和减轻地震灾害，保护人民生命和财产安全，促进经济社会的可持续发展，制定本法。
第二条　在中华人民共和国领域和中华人民共和国管辖的其他海域从事地震监测预报、地震灾害预防、地震应急救援、地震灾后过渡性安置和恢复重建等防震减灾活动，适用本法。
""".encode("utf-8")


def test_parse_legal_txt_builds_tree():
    tree, md5 = parse_to_tree(_legal_text(), filename="防震减灾法.txt", file_type="法律", region="")

    assert tree["doc"]["title"] == "中华人民共和国防震减灾法"
    assert tree["doc"]["meta"].startswith("（1997年12月29日")
    assert len(tree["toc"]) >= 3
    assert tree["tree"][0]["no"] == "第一章"
    assert tree["tree"][0]["children"][0]["no"] == "第一条"
    assert isinstance(md5, str) and len(md5) == 32


def test_iter_legal_chunks_keeps_article_metadata():
    tree, _ = parse_to_tree(_legal_text(), filename="防震减灾法.txt", file_type="法律", region="")
    chunks, metas = iter_legal_chunks(tree)

    assert len(chunks) == 2
    assert metas[0]["chapter_no"] == "第一章"
    assert metas[0]["article_no"] == "第一条"
    assert metas[1]["article_no"] == "第二条"
    assert metas[0]["file_type"] == "法律"


def test_full_width_space_in_toc_is_normalized():
    """"目　　录" 含全角空格，必须被归一成 "目录" 识别掉，否则正文会被当成目录吞掉。"""
    text = """中华人民共和国防震减灾法
（1997年通过）
目　　录
第一章　总　　则
第一条　为了防御和减轻地震灾害，保护人民生命和财产安全，促进经济社会的可持续发展，制定本法。
第二条　适用本法。
""".encode("utf-8")
    tree, _ = parse_to_tree(text, filename="法.txt", file_type="法律", region="")
    assert tree["doc"]["meta"].startswith("（1997年")
    articles = [n["no"] for ch in tree["tree"] for n in ch.get("children", []) if n.get("level") == "article"]
    assert "第一条" in articles and "第二条" in articles


def test_plain_text_wrapped_into_minimal_tree():
    """非法规文本包成最简文档树（单根 article），入库只走这一条路径。"""
    plain = "这是一段普通文本，没有任何章节条结构。\n另一段普通文本。\n".encode("utf-8")
    tree, _ = parse_to_tree(plain, filename="notes.txt", file_type="说明", region="广东省")

    assert tree["toc"] == []
    assert len(tree["tree"]) == 1
    art = tree["tree"][0]
    assert art["level"] == "article" and art["no"] == ""
    assert "普通文本" in art["text"]
    # 仍能照常出块（单块整段），且 metadata 带全
    chunks, metas = iter_legal_chunks(tree)
    assert len(chunks) == 1
    assert metas[0]["doc_title"] == "" and metas[0]["article_no"] == ""
    assert metas[0]["file_type"] == "说明"
    assert metas[0]["region"] == "广东省"


def test_parse_to_tree_rejects_unsupported_ext():
    """未知后缀抛 ValueError；docx 已支持（走 python-docx，非 NotImplementedError）。"""
    import pytest
    with pytest.raises(ValueError):
        parse_to_tree(b"whatever", filename="x.csv", file_type="法律", region="")


def test_pdf_scan_rejected():
    """扫描件/无文本层 PDF 抛明确 ValueError，不建垃圾树。"""
    import pytest
    # 无文本层：合法结构但内容为空的 PDF（reportlab 空页），extract_text 返回空
    try:
        import io

        from reportlab.pdfgen import canvas
        buf = io.BytesIO()
        c = canvas.Canvas(buf)
        c.showPage()
        c.save()
        pdf_bytes = buf.getvalue()
    except ImportError:
        pytest.skip("reportlab 未安装")
    with pytest.raises(ValueError):
        parse_to_tree(pdf_bytes, filename="scan.pdf", file_type="法律", region="")


def test_docx_parsed_to_tree():
    """docx 按段落提取 → 法规结构建树（接缝兑现）。"""
    import io

    import pytest
    try:
        from docx import Document
    except ImportError:
        pytest.skip("python-docx 未安装")
    doc = Document()
    for t in [
        "中华人民共和国防震减灾法",
        "第一章  总则",
        "第一条  为了防御和减轻地震灾害，制定本法。",
        "第二条  适用本法。",
    ]:
        doc.add_paragraph(t)
    buf = io.BytesIO()
    doc.save(buf)

    tree, md5 = parse_to_tree(buf.getvalue(), filename="法规.docx", file_type="法律", region="")
    assert tree["doc"]["title"] == "中华人民共和国防震减灾法"
    assert len(tree["tree"]) >= 1
    assert isinstance(md5, str) and len(md5) == 32


def test_kb_tree_repo_roundtrip_is_faithful():
    """活合同：树落盘 SQLite 再读回，切块结果与原树逐字段一致。"""
    init_db()
    tree, md5 = parse_to_tree(_legal_text(), filename="防震减灾法.txt", file_type="法律", region="")
    kb_tree_repo.save("防震减灾法.txt", tree, md5)

    reloaded = kb_tree_repo.load("防震减灾法.txt")
    assert reloaded is not None
    chunks_before, metas_before = iter_legal_chunks(tree)
    chunks_after, metas_after = iter_legal_chunks(reloaded)
    assert chunks_before == chunks_after
    assert metas_before == metas_after

    assert kb_tree_repo.delete("防震减灾法.txt") is True
    assert kb_tree_repo.load("防震减灾法.txt") is None


# ---------------------------------------------------------------------------
# A. 分段 _split_long_text 补测（既有逻辑此前零覆盖：现有用例文本都短，触发不到切分）
# ---------------------------------------------------------------------------


def test_split_long_text_splits_at_boundary():
    """多句超长条文 → 按 max_chars 边界切成多块，块间无损拼接。"""
    sents = ["第一句" * 60 + "。", "第二句" * 100 + "。", "第三句" * 10 + "。"]
    text = "".join(sents)
    parts = _split_long_text(text, 400)

    assert len(parts) >= 2
    assert all(len(p) <= 400 for p in parts)
    assert "".join(parts) == text  # 句子不被切碎、可无损还原


def test_split_long_text_runon_hardcut():
    """无句读的超长单句 → 按字符硬切成 ≤max_chars 块（保住 embedding 512 token 上限）。"""
    text = "超长句无句读内容" * 100  # 800 字、无任何标点
    parts = _split_long_text(text, 400)

    assert all(len(p) <= 400 for p in parts)
    assert len(parts) == 2
    assert "".join(parts) == text


def test_split_long_text_boundary_flush_logic():
    """句子越过剩余 buf → 前块 flush、新块起（累加判据）。"""
    text = "甲" * 250 + "。" + "乙" * 200 + "。" + "丙" * 60 + "。"
    parts = _split_long_text(text, 400)

    assert parts == ["甲" * 250 + "。", "乙" * 200 + "。" + "丙" * 60 + "。"]


# ---------------------------------------------------------------------------
# B. 非法规文本分节（_parse_plain）
# ---------------------------------------------------------------------------


def test_plain_chinese_heading_builds_sections():
    """中文序号 + 括号序号 → chapter/section 层级树，chunk 带章节元数据。"""
    text = """安全生产大检查方案
一、总体要求
坚持问题导向，全面排查各类隐患。

二、重点任务
（一）危化品检查
核对危化品存储与台账。

（二）消防检查
检查疏散通道与消防设施。

三、保障措施
加强组织领导，落实资金。
""".encode("utf-8")
    tree, _ = parse_to_tree(text, filename="方案.txt", file_type="说明", region="广东省")

    chapters = [n for n in tree["tree"] if n["level"] == "chapter"]
    assert [c["no"] for c in chapters] == ["一、", "二、", "三、"]
    second = chapters[1]
    assert [s["no"] for s in second["children"] if s.get("level") == "section"] == ["（一）", "（二）"]

    chunks, metas = iter_legal_chunks(tree)
    assert chunks   # 有正文
    assert any(m["chapter_no"] == "一、" for m in metas)
    assert any(m.get("section_no") == "（一）" for m in metas)


def test_plain_numeric_heading_levels():
    """1. 一级 + 1.1 二级 → 多级阿拉伯序号识别为层级。"""
    text = """1. 概述
本方案适用于全省化工企业。

1.1 定义
重大隐患指可能导致事故的缺陷或隐患。
""".encode("utf-8")
    tree, _ = parse_to_tree(text, filename="制度.txt", file_type="说明", region="")

    chapters = [n for n in tree["tree"] if n["level"] == "chapter"]
    assert [c["no"] for c in chapters] == ["1"]
    sections = [c for c in chapters[0]["children"] if c.get("level") == "section"]
    assert sections and sections[0]["no"] == "1.1"


def test_plain_long_text_bounded_articles():
    """无结构长文按空行分块 → 多 article，chunk 长度有界。"""
    para = "段落内容" * 60  # 240 字/段
    text = "\n\n".join([para, para, para]).encode("utf-8")
    tree, _ = parse_to_tree(text, filename="notes.txt", file_type="说明", region="")

    chunks, metas = iter_legal_chunks(tree)
    assert len(chunks) == 3          # 每段一块
    assert all(len(c) <= 400 for c in chunks)
    assert all(m["chapter_no"] == "" for m in metas)  # 无结构 → 无章节元数据


def test_plain_single_runon_paragraph_bounded():
    """一整段无标点超长正文（无空行分隔）→ 硬切分块，块有界。"""
    text = ("无结构长句内容" * 200).encode("utf-8")  # 1400 字一坨
    tree, _ = parse_to_tree(text, filename="notes.txt", file_type="说明", region="")

    chunks, _ = iter_legal_chunks(tree)
    assert len(chunks) >= 3
    assert all(len(c) <= 400 for c in chunks)


def test_plain_short_doc_single_article():
    """短无结构文档 → 退回单 article（兼容旧行为：单块整段）。"""
    plain = "这是一段普通文本，没有任何章节条结构。\n另一段普通文本。\n".encode("utf-8")
    tree, _ = parse_to_tree(plain, filename="notes.txt", file_type="说明", region="广东省")

    assert len(tree["tree"]) == 1
    assert tree["tree"][0]["level"] == "article" and tree["tree"][0]["no"] == ""
    chunks, metas = iter_legal_chunks(tree)
    assert len(chunks) == 1
    assert metas[0]["file_type"] == "说明" and metas[0]["region"] == "广东省"


# ---------------------------------------------------------------------------
# C. 分节护栏：完句长句/超长行不被误判为标题
# ---------------------------------------------------------------------------


def test_plain_long_sentence_not_heading():
    """长得像编号的完句长句 → 不算标题，作正文。"""
    text = """本方案适用于以下情形：
1. 针对上述问题，应当从技术、管理、制度三个层面逐项分解责任并闭环督导，确保整改见底见效。
2. 同时应当建立常态化复查机制，防止同类问题反弹回潮。
""".encode("utf-8")
    tree, _ = parse_to_tree(text, filename="notes.txt", file_type="说明", region="")

    # 1./2. 两句带句读（或超长），不应成为 chapter——整篇都应是裸 article 正文
    assert all(n.get("level") == "article" for n in tree["tree"])
    assert all(n.get("no", "") == "" for n in tree["tree"])


def test_plain_numbered_list_items_not_heading():
    """连续编号短项（列表）→ 不当标题、退作正文（防连环空章节）。"""
    text = """1. 清理现场
2. 报告领导
3. 组织撤离
""".encode("utf-8")
    tree, _ = parse_to_tree(text, filename="notes.txt", file_type="说明", region="")

    assert all(n.get("level") == "article" for n in tree["tree"])
    chunks, _ = iter_legal_chunks(tree)
    assert len(chunks) == 1                  # 列表作一段正文，不拆成空章节
    assert "清理现场" in chunks[0] and "组织撤离" in chunks[0]
