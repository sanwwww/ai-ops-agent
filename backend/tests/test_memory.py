"""
tests/test_memory.py：会话记忆（P1）专项测试。

两层验证：
1. memory_store 单元测试：追加/读取/截断/上限
2. API 集成测试：同一 session_id 两轮对话，第二轮 Agent 收到历史上下文；
   done 事件回传 session_id；/api/history 可查持久化消息

注意：把 memory_store.DB_PATH 指到临时目录，不污染真实库。
"""
import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import pytest


@pytest.fixture()
def mem(tmp_path, monkeypatch):
    """隔离的会话存储实例。"""
    import memory_store
    monkeypatch.setattr(memory_store, "DB_PATH", str(tmp_path / "test_memory.db"))
    return memory_store


def test_append_and_get_history(mem):
    mem.append_message("s1", "user", "example.com 打不开")
    mem.append_message("s1", "assistant", "排查结论：DNS 正常")
    h = mem.get_history("s1")
    assert [m["role"] for m in h] == ["user", "assistant"]
    assert "example.com" in h[0]["content"]


def test_history_order_and_limit(mem):
    for i in range(10):
        mem.append_message("s2", "user", f"问题{i}")
        mem.append_message("s2", "assistant", f"回答{i}")
    h = mem.get_history("s2", limit=4)  # 默认 8，这里测 4
    assert len(h) == 4
    assert h[0]["content"] == "问题8"  # 取最近的，且时间正序
    assert h[-1]["content"] == "回答9"


def test_content_truncated(mem):
    mem.append_message("s3", "user", "x" * 5000)
    h = mem.get_history("s3")
    assert len(h[0]["content"]) == 1500


def test_sessions_isolated(mem):
    mem.append_message("a", "user", "A 的问题")
    mem.append_message("b", "user", "B 的问题")
    assert len(mem.get_history("a")) == 1
    assert "A 的问题" in mem.get_history("a")[0]["content"]


# ---------- API 集成 ----------

@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """TestClient + 隔离记忆库 + mock Agent 流。"""
    import main
    import memory_store
    from fastapi.testclient import TestClient

    monkeypatch.setattr(memory_store, "DB_PATH", str(tmp_path / "test_api.db"))

    def fake_stream(msg, history=None, max_turns=6):
        yield ("delta", {"text": f"收到（历史{len(history or [])}条）："})
        yield ("delta", {"text": msg})
        yield ("done", {"steps": [], "elapsed": 0.1})

    monkeypatch.setattr(main, "run_agent_stream", fake_stream)
    return TestClient(main.app)


def _sse_events(text):
    events = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        lines = block.strip().split("\n")
        ev = dict(l.split(": ", 1) for l in lines)
        events.append((ev["event"], json.loads(ev["data"])))
    return events


def test_two_turns_share_memory(api_client):
    """同一 session_id 第二轮提问时，Agent 应收到第一轮的问答历史。"""
    r1 = api_client.post("/api/chat/stream", json={"message": "第一轮问题", "session_id": "sess-x"})
    done1 = [d for e, d in _sse_events(r1.text) if e == "done"][0]
    assert done1["session_id"] == "sess-x"

    r2 = api_client.post("/api/chat/stream", json={"message": "刚才我问的是什么？", "session_id": "sess-x"})
    # 从 delta 拼出回答，fake_stream 会回显历史条数
    answer = "".join(d["text"] for e, d in _sse_events(r2.text) if e == "delta")
    assert "历史2条" in answer  # 第一轮的 user + assistant


def test_different_sessions_isolated(api_client):
    api_client.post("/api/chat/stream", json={"message": "A 会话", "session_id": "sess-a"})
    r2 = api_client.post("/api/chat/stream", json={"message": "B 会话", "session_id": "sess-b"})
    answer = "".join(d["text"] for e, d in _sse_events(r2.text) if e == "delta")
    assert "历史0条" in answer  # 不同会话互相看不到


def test_history_endpoint(api_client):
    api_client.post("/api/chat/stream", json={"message": "查一下 DNS", "session_id": "sess-h"})
    r = api_client.get("/api/history", params={"session_id": "sess-h"})
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "查一下 DNS"


def test_generated_session_id_returned(api_client):
    """不传 session_id 时服务端生成，done 事件带回。"""
    r = api_client.post("/api/chat/stream", json={"message": "没有 ID 的会话"})
    done = [d for e, d in _sse_events(r.text) if e == "done"][0]
    assert done.get("session_id")  # 有生成值
