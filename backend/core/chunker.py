"""文本编码与指纹工具 — 知识库摄取的公共底层。

切块逻辑已迁入 legal_parser（解析→文档树→切块统一路径），本模块只保留：
    - decode_text / read_text：兼容 utf-8 / gbk 编码
    - file_md5：归一化文本内容指纹，判文件是否变化
"""

import hashlib


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
