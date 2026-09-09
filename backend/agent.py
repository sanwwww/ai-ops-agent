"""
Agent 核心：整个项目最关键的一个文件。
它实现的就是"Agent 循环"（也叫 ReAct 循环）：

    LLM 思考 -> 决定调用哪个工具 -> 执行工具 -> 把结果喂回 LLM -> 再思考 -> ... -> 给出最终回答

普通 LLM 应用只走一步（提问->回答）；
Agent 能"决定下一步做什么"，区别就在这个 while 循环里。
不用 LangChain，手写循环反而更适合面试时讲清楚原理。
"""
import os
import json
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


def run_agent(user_message: str, max_turns: int = 6) -> dict:
    """跑一轮完整的 Agent 循环，返回最终答案 + 每一步工具调用记录（给前端展示）。"""
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return {"answer": "请先在 backend/.env 中配置 DEEPSEEK_API_KEY（可去 platform.deepseek.com 申请）。", "steps": []}

    client = OpenAI(api_key=api_key, base_url=BASE_URL)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]
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
