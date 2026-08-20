"""chunker 的单元测试。

重点验证两件事：
1. 法规/标准文本会优先按章/条切块
2. 普通段落文本仍然按段落切块
"""

from backend.core.chunker import split_text


def test_split_text_prefers_legal_structure():
    text = (
        "第一章 总则\n"
        "第一条 为了加强安全生产工作，防止和减少生产安全事故，保障人民群众生命和财产安全。\n"
        "第二条 生产经营单位应当建立健全并落实全员安全生产责任制。\n"
        "第二章 责任\n"
        "第三条 生产经营单位的主要负责人对本单位安全生产工作全面负责。"
    )

    chunks = split_text(text, max_chars=200)

    assert len(chunks) == 3
    assert chunks[0].startswith("第一章 总则")
    assert "第一条" in chunks[0]
    assert chunks[1].startswith("第一章 总则")
    assert "第二条" in chunks[1]
    assert chunks[2].startswith("第二章 责任")
    assert "第三条" in chunks[2]


def test_split_text_keeps_paragraph_mode():
    text = (
        "事故经过：现场作业人员未按规定佩戴防护装备，导致高处坠落。\n\n"
        "原因分析：安全教育不到位，现场监护缺失。\n\n"
        "整改建议：立即补充培训，完善巡查机制。"
    )

    chunks = split_text(text, max_chars=50)

    assert len(chunks) == 3
    assert chunks[0].startswith("事故经过")
    assert chunks[1].startswith("原因分析")
    assert chunks[2].startswith("整改建议")
