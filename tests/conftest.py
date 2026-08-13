"""pytest 全局配置 — 让测试文件能 import backend 包。

backend 目录没有 __init__.py（命名空间包），
运行时靠 main.py 里的 sys.path.insert 找到它。
测试也要做同样的事：把项目根目录（SafeRAG/）加进 sys.path。
"""

import os
import sys

# 项目根目录 = 本文件的上两级（tests/ 的上级）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
