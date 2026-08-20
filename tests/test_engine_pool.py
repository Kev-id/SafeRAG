"""qwen_client 引擎池轮询的单元测试。

验证 _next_engine 轮询的确定性：连取 N 次恰好覆盖全部引擎各一次。
"""

import pytest

from backend.core import qwen_client


@pytest.mark.parametrize(
    "urls",
    [
        ["http://127.0.0.1:8000"],
        ["http://127.0.0.1:8000", "http://127.0.0.1:8001"],
        ["http://127.0.0.1:8000", "http://127.0.0.1:8001", "http://127.0.0.1:8002"],
    ],
)
def test_next_engine_round_robin(urls, monkeypatch):
    """连取 len(urls) 次，恰好覆盖全部引擎各一次；下一轮回到起点。"""
    monkeypatch.setattr(qwen_client, "QWEN_BASE_URLS", urls)
    qwen_client._rr_index = 0

    seen = [qwen_client._next_engine() for _ in urls]
    # 每轮 (index, url) 严格对应列表顺序
    assert seen == list(enumerate(urls))

    # 转完一圈后回到第一台引擎
    idx, url = qwen_client._next_engine()
    assert idx == 0
    assert url == urls[0]


def test_two_workers_get_different_engines(monkeypatch):
    """两个并发 worker 各取一台引擎（模拟 chat 前的一次 _next_engine 调用）。"""
    urls = ["http://127.0.0.1:8000", "http://127.0.0.1:8001"]
    monkeypatch.setattr(qwen_client, "QWEN_BASE_URLS", urls)
    qwen_client._rr_index = 0

    _, url_a = qwen_client._next_engine()
    _, url_b = qwen_client._next_engine()
    assert url_a != url_b
