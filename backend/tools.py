"""
网络检测工具：Agent 的"手"。
每个函数就是 Agent 可以调用的一个工具（Tool Calling 里的 Tool）。
安全原则：只做只读检测（ping/DNS/端口/HTTP），不执行任何危险命令。
"""
import socket
import subprocess
import platform
import urllib.request
import urllib.error

from knowledge_base import search as _kb_search


def ping_host(host: str) -> str:
    """检测网络连通性：能不能 ping 通目标。

    降级设计：部分容器环境（如 Render）没有 ping 命令，
    自动改用 TCP 探测（443/80）判断连通性——结果含义不变：目标可达/不可达。
    """
    flag = "-n" if platform.system() == "Windows" else "-c"
    try:
        # Windows 中文系统 ping 输出是 GBK 编码，指定编码防止解码崩溃
        kwargs = {"capture_output": True, "timeout": 15,
                  "encoding": "gbk" if platform.system() == "Windows" else "utf-8",
                  "errors": "replace"}
        r = subprocess.run(["ping", flag, "2", host], **kwargs)
        out = (r.stdout or r.stderr or "").strip()
        return out[-800:]  # 只取末尾，控制返回长度
    except FileNotFoundError:
        # 容器无 ping 命令 -> TCP 探测兜底（ICMP 被禁不影响 TCP 可达性判断）
        for port in (443, 80):
            try:
                with socket.create_connection((host, port), timeout=3):
                    return (f"当前环境无 ping 命令，已改用 TCP 探测："
                            f"{host}:{port} 可连接 → 目标网络可达。"
                            f"注意：ICMP 被禁用时 ping 不通但服务仍正常。")
            except OSError:
                continue
        return (f"当前环境无 ping 命令，TCP 443/80 探测均失败："
                f"{host} 网络不可达或防火墙拦截。")
    except Exception as e:
        return f"ping 执行失败: {e}"


def dns_lookup(domain: str) -> str:
    """DNS 解析：域名能不能解析成 IP。"""
    try:
        ip = socket.gethostbyname(domain)
        return f"DNS 正常：{domain} -> {ip}"
    except Exception as e:
        return f"DNS 解析失败：{domain}，原因: {e}"


def check_port(host: str, port: int) -> str:
    """TCP 端口检测：目标端口有没有服务在监听。"""
    try:
        with socket.create_connection((host, port), timeout=3):
            return f"端口正常：{host}:{port} 可连接"
    except Exception as e:
        return f"端口不通：{host}:{port} 连接失败 ({e})"


def http_check(url: str) -> str:
    """HTTP 服务检测：网站返回什么状态码。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ops-agent/1.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            return f"HTTP 正常：{resp.status} {resp.reason}"
    except urllib.error.HTTPError as e:
        return f"HTTP 异常：返回 {e.code} {e.reason}"
    except Exception as e:
        return f"HTTP 请求失败：{e}"


def search_knowledge(query: str) -> str:
    """RAG 检索：从本地运维知识库中按语义查找相关排障经验（只读，无副作用）。"""
    return _kb_search(query)


# ---------- 下面是给 LLM 看的"工具说明书"（Tool Schema） ----------
# LLM 就是靠读这段 JSON 描述，才知道自己有哪些工具、什么时候该调哪个。

TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "dns_lookup",
        "description": "检测域名能否解析为 IP，用于排查 DNS 问题",
        "parameters": {"type": "object", "properties": {
            "domain": {"type": "string", "description": "要检测的域名，如 example.com"},
        }, "required": ["domain"]},
    }},
    {"type": "function", "function": {
        "name": "ping_host",
        "description": "ping 目标主机，检测网络连通性",
        "parameters": {"type": "object", "properties": {
            "host": {"type": "string", "description": "主机名或 IP"},
        }, "required": ["host"]},
    }},
    {"type": "function", "function": {
        "name": "check_port",
        "description": "检测目标主机的 TCP 端口是否可连接（如 80/443/22/3306）",
        "parameters": {"type": "object", "properties": {
            "host": {"type": "string", "description": "主机名或 IP"},
            "port": {"type": "integer", "description": "端口号"},
        }, "required": ["host", "port"]},
    }},
    {"type": "function", "function": {
        "name": "http_check",
        "description": "请求一个 URL，检查 HTTP 服务返回的状态码（排查 502/503 等）",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "完整 URL，如 https://example.com"},
        }, "required": ["url"]},
    }},
    {"type": "function", "function": {
        "name": "search_knowledge",
        "description": "检索运维知识库（RAG），查找相关故障的排查经验和方法论。"
                       "在分析检测结果、不确定下一步怎么查、或需要解释故障原理时调用",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "要检索的问题，如 'ping 不通但网站能打开' 或 'HTTP 502 排查'"},
        }, "required": ["query"]},
    }},
]

# 工具名 -> 真正的 Python 函数，Agent 循环按名字从这里取函数执行
TOOL_FUNCTIONS = {
    "ping_host": ping_host,
    "dns_lookup": dns_lookup,
    "check_port": check_port,
    "http_check": http_check,
    "search_knowledge": search_knowledge,
}
