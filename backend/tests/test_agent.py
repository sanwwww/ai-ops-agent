"""
tests/test_agent.py：Agent 循环测试（核心）。

用假的 LLM 客户端（mock）驱动 run_agent 跑完一个完整的
"调工具 -> 喂回结果 -> 给最终回答" 循环，不花一分钱 API 费用。
面试可以讲：Agent 循环逻辑用单测覆盖，LLM 本身不测（不可控），
边界用 mock 隔离——这是 Agent 项目测试的标准做法。
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import agent
from agent import run_agent


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, id, name, arguments):
        self.id = id
        self.function = FakeFunction(name, arguments)


class FakeResponse:
    def __init__(self, message):
        self.choices = [type("C", (), {"message": message})()]


class FakeCompletions:
    """按脚本顺序吐出预设的 LLM 响应，模拟真实多轮循环。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(self.script.pop(0))


class FakeClient:
    def __init__(self, script):
        self.chat = type("Chat", (), {})()
        self.chat.completions = FakeCompletions(script)


def test_agent_tool_loop(monkeypatch):
    """完整循环：第 1 轮 LLM 决定调 dns_lookup -> 结果写回 -> 第 2 轮给最终回答。"""
    fake = FakeClient([
        FakeMessage(tool_calls=[FakeToolCall("call_1", "dns_lookup",
                                             '{"domain": "example.com"}')]),
        FakeMessage(content="排查完成：DNS 解析正常。"),
    ])
    monkeypatch.setattr(agent, "_get_client", lambda: (fake, None))

    result = run_agent("example.com 打不开")

    assert result["answer"] == "排查完成：DNS 解析正常。"
    assert len(result["steps"]) == 1
    assert result["steps"][0]["tool"] == "dns_lookup"
    assert "DNS" in result["steps"][0]["result"]
    # 工具结果必须以 role=tool 消息写回对话历史（Agent 循环的关键约定）
    tool_msgs = [m for m in fake.chat.completions.calls[1]["messages"]
                 if isinstance(m, dict) and m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "call_1"


def test_agent_unknown_tool_is_safe(monkeypatch):
    """LLM 点了一个白名单外的工具名 -> 不能执行，只返回提示。"""
    fake = FakeClient([
        FakeMessage(tool_calls=[FakeToolCall("call_1", "rm_rf_slash", "{}")]),
        FakeMessage(content="好的。"),
    ])
    monkeypatch.setattr(agent, "_get_client", lambda: (fake, None))

    result = run_agent("删库")

    assert "未知工具" in result["steps"][0]["result"]


def test_agent_max_turns_guard(monkeypatch):
    """max_turns=6 防死循环：LLM 一直要求调工具时，循环必须能退出。"""
    loop_resp = FakeMessage(tool_calls=[FakeToolCall("c", "search_knowledge",
                                                     '{"query": "x"}')])
    fake = FakeClient([loop_resp] * 10)
    monkeypatch.setattr(agent, "_get_client", lambda: (fake, None))

    result = run_agent(" looping", max_turns=3)

    assert len(fake.chat.completions.calls) <= 3
    assert "最大排查轮数" in result["answer"]


def test_agent_no_key_friendly_error(monkeypatch):
    """API Key 未配置：返回友好提示而不是崩溃。"""
    monkeypatch.setattr(agent, "_get_client", lambda: (None, {"answer": "请先配置", "steps": []}))
    result = run_agent("hi")
    assert result["answer"] == "请先配置"
