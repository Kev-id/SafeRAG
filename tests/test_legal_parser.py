"""legal_parser + kb_tree_repo 的单元测试。

覆盖三件事：
1. 法规 txt（标题/说明/目录/章/条）→ 层级文档树，全角空格目录探测正确。
2. 非法规文本 → 包成最简文档树（单根 article），与法规走同一条入库路径。
3. 文档树经 kb_tree_repo 落盘 SQLite 再读回，与切块忠实——活合同成立。

parse_to_tree 吃 bytes（按后缀分派），故测试输入统一 encode。
"""

import os
import tempfile

# 必须在 import database 之前：把测试库指向临时文件，隔离生产 saferag.db
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP_DB.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.name}"

from backend.core.database import init_db  # noqa: E402
from backend.core.legal_parser import iter_legal_chunks, parse_to_tree  # noqa: E402
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
    tree, md5 = parse_to_tree(_legal_text(), filename="防震减灾法.txt", file_type="法律")

    assert tree["doc"]["title"] == "中华人民共和国防震减灾法"
    assert tree["doc"]["meta"].startswith("（1997年12月29日")
    assert len(tree["toc"]) >= 3
    assert tree["tree"][0]["no"] == "第一章"
    assert tree["tree"][0]["children"][0]["no"] == "第一条"
    assert isinstance(md5, str) and len(md5) == 32


def test_iter_legal_chunks_keeps_article_metadata():
    tree, _ = parse_to_tree(_legal_text(), filename="防震减灾法.txt", file_type="法律")
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
    tree, _ = parse_to_tree(text, filename="法.txt", file_type="法律")
    assert tree["doc"]["meta"].startswith("（1997年")
    articles = [n["no"] for ch in tree["tree"] for n in ch.get("children", []) if n.get("level") == "article"]
    assert "第一条" in articles and "第二条" in articles


def test_plain_text_wrapped_into_minimal_tree():
    """非法规文本包成最简文档树（单根 article），入库只走这一条路径。"""
    plain = "这是一段普通文本，没有任何章节条结构。\n另一段普通文本。\n".encode("utf-8")
    tree, _ = parse_to_tree(plain, filename="notes.txt", file_type="说明")

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


def test_parse_to_tree_rejects_unsupported_ext():
    """未知后缀抛 ValueError；docx 已支持（走 python-docx，非 NotImplementedError）。"""
    import pytest
    with pytest.raises(ValueError):
        parse_to_tree(b"whatever", filename="x.csv", file_type="法律")


def test_pdf_scan_rejected():
    """扫描件/无文本层 PDF 抛明确 ValueError，不建垃圾树。"""
    import pytest
    # 无文本层：合法结构但内容为空的 PDF（reportlab 空页），extract_text 返回空
    try:
        from reportlab.pdfgen import canvas
        import io
        buf = io.BytesIO()
        c = canvas.Canvas(buf)
        c.showPage()
        c.save()
        pdf_bytes = buf.getvalue()
    except ImportError:
        pytest.skip("reportlab 未安装")
    with pytest.raises(ValueError):
        parse_to_tree(pdf_bytes, filename="scan.pdf", file_type="法律")


def test_docx_parsed_to_tree():
    """docx 按段落提取 → 法规结构建树（接缝兑现）。"""
    import pytest
    import io
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

    tree, md5 = parse_to_tree(buf.getvalue(), filename="法规.docx", file_type="法律")
    assert tree["doc"]["title"] == "中华人民共和国防震减灾法"
    assert len(tree["tree"]) >= 1
    assert isinstance(md5, str) and len(md5) == 32


def test_kb_tree_repo_roundtrip_is_faithful():
    """活合同：树落盘 SQLite 再读回，切块结果与原树逐字段一致。"""
    init_db()
    tree, md5 = parse_to_tree(_legal_text(), filename="防震减灾法.txt", file_type="法律")
    kb_tree_repo.save("防震减灾法.txt", tree, md5)

    reloaded = kb_tree_repo.load("防震减灾法.txt")
    assert reloaded is not None
    chunks_before, metas_before = iter_legal_chunks(tree)
    chunks_after, metas_after = iter_legal_chunks(reloaded)
    assert chunks_before == chunks_after
    assert metas_before == metas_after

    assert kb_tree_repo.delete("防震减灾法.txt") is True
    assert kb_tree_repo.load("防震减灾法.txt") is None
