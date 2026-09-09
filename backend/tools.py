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


def ping_host(host: str) -> str:
    """检测网络连通性：能不能 ping 通目标。"""
    flag = "-n" if platform.system() == "Windows" else "-c"
    try:
        r = subprocess.run(
            ["ping", flag, "2", host],
            capture_output=True, text=True, timeout=15,
        )
        out = (r.stdout or r.stderr).strip()
        return out[-800:]  # 只取末尾，控制返回长度
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
]

# 工具名 -> 真正的 Python 函数，Agent 循环按名字从这里取函数执行
TOOL_FUNCTIONS = {
    "ping_host": ping_host,
    "dns_lookup": dns_lookup,
    "check_port": check_port,
    "http_check": http_check,
}
