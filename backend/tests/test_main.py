"""
tests/test_main.py：API 层测试。

用 FastAPI 官方 TestClient（httpx 驱动），mock 掉 Agent 层，
只测 API 自身逻辑：限流 / 输入校验 / SSE 格式 / 健康检查。
"""
import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import main
from fastapi.testclient import TestClient

client = TestClient(main.app)


def _mock_stream(monkeypatch):
    """把 Agent 层替换成固定事件流的假实现。"""
    def fake_stream(msg, max_turns=6):
        yield ("status", {"stage": "正在分析问题"})
        yield ("tool_start", {"tool": "dns_lookup", "args": {"domain": "example.com"}})
        yield ("tool_result", {"tool": "dns_lookup", "args": {"domain": "example.com"}, "result": "ok"})
        yield ("delta", {"text": "结论："})
        yield ("delta", {"text": "正常"})
        yield ("done", {"steps": [], "elapsed": 0.1})

    monkeypatch.setattr(main, "run_agent_stream", fake_stream)


def test_health_includes_kb_status():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "kb_chunks" in body  # 可观测性：健康检查带知识库状态
    assert body["kb_chunks"] >= 0


def test_message_too_long_rejected():
    r = client.post("/api/chat", json={"message": "a" * 501})
    assert r.status_code == 400


def test_request_id_header_present(monkeypatch):
    """每个响应都带 X-Request-ID（请求追踪）。"""
    _mock_stream(monkeypatch)
    r = client.post("/api/chat", json={"message": "hi"})
    assert "x-request-id" in r.headers


def test_sse_stream_format(monkeypatch):
    """SSE 主接口：事件必须是 'event: xxx\\ndata: {...}\\n\\n' 格式且按序到达。"""
    _mock_stream(monkeypatch)
    main._requests.clear()  # 清空限流窗口，避免被前面的测试触发 429
    r = client.post("/api/chat/stream", json={"message": "hi"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    events = []
    for block in r.text.split("\n\n"):
        if not block.strip():
            continue
        lines = block.strip().split("\n")
        ev = dict(l.split(": ", 1) for l in lines)
        events.append((ev["event"], json.loads(ev["data"])))

    names = [e[0] for e in events]
    assert names == ["status", "tool_start", "tool_result", "delta", "delta", "done"]


def test_rate_limit(monkeypatch):
    """限流：同一 IP 超过每分钟上限 -> 429。"""
    _mock_stream(monkeypatch)
    main._requests.clear()
    original = main.RATE_LIMIT
    main.RATE_LIMIT = 2  # 临时调低便于测试
    try:
        for _ in range(2):
            r = client.post("/api/chat/stream", json={"message": "hi"})
            assert r.status_code == 200
        r = client.post("/api/chat/stream", json={"message": "hi"})
        assert r.status_code == 429
    finally:
        main.RATE_LIMIT = original
        main._requests.clear()
