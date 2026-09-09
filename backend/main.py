"""
FastAPI 后端入口：Agent 的"门面"。
职责：接收前端的 HTTP 请求 -> 交给 Agent 处理 -> 返回 JSON。

上线安全防护（公网部署必备，面试可以讲）：
1. 限流：每个 IP 每分钟最多 10 次请求，防止 API Key 被恶意刷爆
2. 消息长度限制：最长 500 字符，防止超长输入浪费 Token
3. PORT 环境变量：适配云平台（Render/Railway 等会动态分配端口）
"""
import os
import time
from collections import defaultdict, deque

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from agent import run_agent

load_dotenv()  # 本地运行时读取 .env 里的 API Key；线上用平台环境变量

app = FastAPI(title="AI Ops Agent")

MAX_MESSAGE_LEN = 500   # 单条消息最大长度
RATE_LIMIT = 10         # 每个 IP 每分钟最大请求数
_requests: dict = defaultdict(deque)  # {ip: [时间戳队列]}


def _check_rate_limit(ip: str):
    """滑动窗口限流：60 秒内超过 RATE_LIMIT 次就拒绝。"""
    now = time.time()
    q = _requests[ip]
    while q and q[0] < now - 60:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="请求太频繁，请一分钟后再试")
    q.append(now)


class ChatRequest(BaseModel):
    message: str  # Pydantic 自动校验请求体格式


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request):
    """前端聊天接口：POST {"message": "..."} -> Agent 分析 -> {"answer", "steps"}"""
    _check_rate_limit(request.client.host)
    if len(req.message) > MAX_MESSAGE_LEN:
        raise HTTPException(status_code=400, detail=f"消息过长，最多 {MAX_MESSAGE_LEN} 字符")
    try:
        return run_agent(req.message)
    except Exception as e:
        return {"answer": f"服务出错: {e}", "steps": []}


@app.get("/health")
def health():
    """健康检查接口（运维项目给自己加个 health check，面试加分）"""
    return {"status": "ok"}


@app.get("/")
def index():
    """首页：直接返回聊天页面（单文件前端，不用 npm 构建）"""
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


if __name__ == "__main__":
    import uvicorn
    # 本地默认 8000；云平台通过 PORT 环境变量注入真实端口
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
