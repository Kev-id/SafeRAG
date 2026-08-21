"""legal_parser 的单元测试。

只覆盖最典型的法规 txt 格式：
标题 + 说明 + 目录 + 章 + 条。
"""

from backend.core.legal_parser import build_chunks, looks_like_legal_txt, parse_legal_txt, iter_legal_chunks


def test_parse_legal_txt_builds_tree():
    text = """中华人民共和国防震减灾法
（1997年12月29日第八届全国人民代表大会常务委员会第二十九次会议通过　2008年12月27日第十一届全国人民代表大会常务委员会第六次会议修订）
目　　录
第一章　总　　则
第二章　防震减灾规划
第三章　地震监测预报
第一章　总　　则
第一条　为了防御和减轻地震灾害，保护人民生命和财产安全，促进经济社会的可持续发展，制定本法。
第二条　在中华人民共和国领域和中华人民共和国管辖的其他海域从事地震监测预报、地震灾害预防、地震应急救援、地震灾后过渡性安置和恢复重建等防震减灾活动，适用本法。
"""

    assert looks_like_legal_txt(text)

    tree = parse_legal_txt(text, source="防震减灾法.txt", file_type="法律")

    assert tree["doc"]["title"] == "中华人民共和国防震减灾法"
    assert tree["doc"]["meta"].startswith("（1997年12月29日")
    assert len(tree["toc"]) >= 3
    assert tree["tree"][0]["no"] == "第一章"
    assert tree["tree"][0]["children"][0]["no"] == "第一条"


def test_iter_legal_chunks_keeps_article_metadata():
    text = """中华人民共和国防震减灾法
（1997年12月29日第八届全国人民代表大会常务委员会第二十九次会议通过　2008年12月27日第十一届全国人民代表大会常务委员会第六次会议修订）
目　　录
第一章　总　　则
第一章　总　　则
第一条　为了防御和减轻地震灾害，保护人民生命和财产安全，促进经济社会的可持续发展，制定本法。
第二条　在中华人民共和国领域和中华人民共和国管辖的其他海域从事地震监测预报、地震灾害预防、地震应急救援、地震灾后过渡性安置和恢复重建等防震减灾活动，适用本法。
"""

    tree = parse_legal_txt(text, source="防震减灾法.txt", file_type="法律")
    chunks, metas = iter_legal_chunks(tree)

    assert len(chunks) == 2
    assert metas[0]["chapter_no"] == "第一章"
    assert metas[0]["article_no"] == "第一条"
    assert metas[1]["article_no"] == "第二条"
    # 法规块一定带 file_type（与非法规块 schema 对齐，便于 retriever 统一过滤）
    assert metas[0]["file_type"] == "法律"


def test_full_width_space_in_toc_is_normalized():
    """"目　　录" 含全角空格，必须被归一成 "目录" 识别掉，否则正文会被当成目录吞掉。"""
    text = """中华人民共和国防震减灾法
（1997年通过）
目　　录
第一章　总　　则
第一条　为了防御和减轻地震灾害，保护人民生命和财产安全，促进经济社会的可持续发展，制定本法。
第二条　适用本法。
"""
    tree = parse_legal_txt(text, source="法.txt", file_type="法律")
    # 目录行不该污染 meta 判断；正文两条都该进树
    assert tree["doc"]["meta"].startswith("（1997年")
    articles = [n["no"] for ch in tree["tree"] for n in ch.get("children", []) if n.get("level") == "article"]
    assert "第一条" in articles and "第二条" in articles


def test_build_chunks_legal_vs_plain_dispatches():
    """公共入口：法规文本走结构化路径带 metadata，纯文本走 split_text 不带。"""
    legal_text = "安全生产法\n（2021修正）\n目　　录\n第一章　总则\n第一条　生产经营单位应当具备安全生产条件。\n第二条　矿山应当具备安全生产条件。\n第三条　建筑施工应当具备安全生产条件。\n"
    chunks_l, metas_l, tree_l = build_chunks(legal_text, source="a.txt", file_type="法律")
    assert tree_l is not None
    assert metas_l is not None and metas_l[0]["article_no"] == "第一条"

    plain_text = "这是一段普通文本，没有章节条结构。\n另一段普通文本。\n"
    chunks_p, metas_p, tree_p = build_chunks(plain_text, source="b.txt", file_type="法律")
    assert tree_p is None
    assert metas_p is None
    assert len(chunks_p) >= 1
