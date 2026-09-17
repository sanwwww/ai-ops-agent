"""
tests/test_tools.py：工具层测试。

重点测 RAG 检索和工具白名单，网络类工具只做本机回环测试，
不依赖外网（保证测试在任何环境都能跑）。
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from tools import TOOL_FUNCTIONS, TOOLS_SCHEMA, search_knowledge, dns_lookup


def test_search_knowledge_returns_relevant_content():
    """RAG：查询 502 相关问题，应命中知识库里的 502 排查片段。"""
    result = search_knowledge("网站报 502 错误怎么排查")
    assert "502" in result
    assert "【" in result  # 结果带来源标注（source · title）


def test_search_knowledge_is_readonly_and_bounded():
    """RAG：结果必须截断在限长内，防止撑爆 LLM 上下文。"""
    result = search_knowledge("ping 不通")
    assert len(result) <= 1200


def test_tool_functions_whitelist_complete():
    """安全红线：TOOL_FUNCTIONS 白名单必须和 TOOLS_SCHEMA 一一对应，
    LLM 只能点名白名单里的工具。"""
    schema_names = {t["function"]["name"] for t in TOOLS_SCHEMA}
    assert schema_names == set(TOOL_FUNCTIONS.keys())
    assert "search_knowledge" in schema_names


def test_dns_lookup_localhost():
    """不依赖外网：localhost 一定能解析。"""
    result = dns_lookup("localhost")
    assert "DNS 正常" in result
