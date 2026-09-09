"""
Agent 核心：整个项目最关键的一个文件。
它实现的就是"Agent 循环"（也叫 ReAct 循环）：

    LLM 思考 -> 决定调用哪个工具 -> 执行工具 -> 把结果喂回 LLM -> 再思考 -> ... -> 给出最终回答

普通 LLM 应用只走一步（提问->回答）；
Agent 能"决定下一步做什么"，区别就在这个循环里。
不用 LangChain，手写循环反而更适合面试时讲清楚原理。

两个入口：
- run_agent()        非流式：一次性返回全部结果（保留作兜底/简单调用）
- run_agent_stream() 流式：每一步实时 yield 事件，配合 SSE 推给前端
  事件类型：status / delta（文本增量）/ tool_start / tool_result / done / error
"""
import os
import json
import time
from openai import OpenAI
from tools import TOOLS_SCHEMA, TOOL_FUNCTIONS

SYSTEM_PROMPT = """你是一名 IT 运维助手，擅长 Linux 问题分析、网络故障排查和日志分析。

回答时遵循以下流程：
1. 先判断问题类型（DNS / 网络 / 端口 / HTTP服务 / 系统资源）
2. 需要实际检测时，调用提供的工具获取真实结果，不要凭空猜测
3. 按标准排查链路逐层分析：DNS -> 网络连通性 -> 端口 -> HTTP 服务
4. 最后给出：当前判断 + 可能原因 + 下一步排查命令
5. 对危险操作（如 rm、格式化、改防火墙）必须提醒风险
"""

MODEL = "deepseek-chat"
BASE_URL = "https://api.deepseek.com"  # 兼容 OpenAI 格式，换其他模型只改这两行


def _get_client():
    """返回 (客户端, 错误信息)。Key 未配置时给出友好提示而不是崩溃。"""
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return None, {"answer": "请先在 backend/.env 中配置 DEEPSEEK_API_KEY（可去 platform.deepseek.com 申请）。", "steps": []}
    return OpenAI(api_key=api_key, base_url=BASE_URL), None


def _init_messages(user_message: str) -> list:
    """LLM 没有记忆，全部上下文靠这个列表传递：system 立规矩 + user 提问题。"""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]


def run_agent(user_message: str, max_turns: int = 6) -> dict:
    """非流式：跑一轮完整的 Agent 循环，返回最终答案 + 每一步工具调用记录。"""
    client, err = _get_client()
    if err:
        return err

    messages = _init_messages(user_message)
    steps = []  # 记录 Agent 每一步调用了什么工具、结果是什么

    for _ in range(max_turns):
        # 第 1 步：把对话历史交给 LLM，让它思考
        resp = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS_SCHEMA,  # 告诉 LLM 有哪些工具可用
        )
        msg = resp.choices[0].message

        # 第 2 步：LLM 如果决定调用工具 -> 执行它
        if msg.tool_calls:
            messages.append(msg)  # LLM 的"决策"也要存回历史
            for tc in msg.tool_calls:
                name = tc.function.name
                args = json.loads(tc.function.arguments)
                func = TOOL_FUNCTIONS.get(name)
                result = func(**args) if func else f"未知工具: {name}"
                steps.append({"tool": name, "args": args, "result": result})
                # 第 3 步：把工具结果喂回给 LLM，让它继续分析
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        else:
            # 第 4 步：LLM 不再调用工具，说明分析完了，返回最终回答
            return {"answer": msg.content, "steps": steps}

    return {"answer": "已达最大排查轮数，请根据以上结果继续排查。", "steps": steps}


def run_agent_stream(user_message: str, max_turns: int = 6):
    """流式版 Agent 循环：每一步实时 yield (事件名, 数据)，由 SSE 推给前端。

    与非流式版本的唯一区别：LLM 的回答是逐字接收的（stream=True），
    所以工具调用参数也是分片到达的，需要自己拼接（tool_acc 字典干这事）。
    """
    client, err = _get_client()
    if err:
        yield ("error", err)
        return

    messages = _init_messages(user_message)
    steps = []
    start = time.time()
    yield ("status", {"stage": "正在分析问题"})

    for _ in range(max_turns):
        stream = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS_SCHEMA,
            stream=True,  # 关键：开启流式，回答逐字返回
        )

        content_parts = []   # 本轮 LLM 说的话（可能边说边决定调工具）
        tool_acc = {}        # 拼装分片到达的工具调用请求 {index: {id, name, args}}

        for chunk in stream:
            choice = chunk.choices[0]
            delta = choice.delta
            if delta and delta.content:
                content_parts.append(delta.content)
                yield ("delta", {"text": delta.content})  # 实时推给前端
            if delta and delta.tool_calls:
                for tc in delta.tool_calls:
                    acc = tool_acc.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        acc["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            acc["name"] += tc.function.name
                        if tc.function.arguments:
                            acc["args"] += tc.function.arguments

        # 本轮 LLM 想调工具 -> 我们的代码执行（LLM 只点名，不干活）
        if tool_acc:
            messages.append({
                "role": "assistant",
                "content": "".join(content_parts) or None,
                "tool_calls": [
                    {"id": a["id"], "type": "function",
                     "function": {"name": a["name"], "arguments": a["args"]}}
                    for a in tool_acc.values()
                ],
            })
            for a in tool_acc.values():
                name = a["name"]
                try:
                    args = json.loads(a["args"]) if a["args"] else {}
                except json.JSONDecodeError:
                    args = {}
                yield ("tool_start", {"tool": name, "args": args})
                func = TOOL_FUNCTIONS.get(name)
                result = func(**args) if func else f"未知工具: {name}"
                steps.append({"tool": name, "args": args, "result": result})
                yield ("tool_result", {"tool": name, "args": args, "result": result})
                messages.append({"role": "tool", "tool_call_id": a["id"], "content": result})
            continue  # 带着结果进入下一轮

        # 没有工具调用 -> 分析完成（内容已通过 delta 实时推完）
        yield ("done", {"steps": steps, "elapsed": round(time.time() - start, 1)})
        return

    yield ("done", {"answer": "已达最大排查轮数，请根据以上结果继续排查。",
                    "steps": steps, "elapsed": round(time.time() - start, 1)})
