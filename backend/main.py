"""
FastAPI 后端入口：Agent 的"门面"。
职责：接收前端的 HTTP 请求 -> 交给 Agent 处理 -> 返回 JSON。

上线安全防护（公网部署必备，面试可以讲）：
1. 限流：每个 IP 每分钟最多 10 次请求，防止 API Key 被恶意刷爆
2. 消息长度限制：最长 500 字符，防止超长输入浪费 Token
3. PORT 环境变量：适配云平台（Render/Railway 等会动态分配端口）

可观测性（Observability，面试常问"线上出问题你怎么查"）：
1. 结构化日志：每条请求记录 方法/路径/状态码/耗时，异常带堆栈
2. 请求追踪 ID：每个请求生成唯一 request_id，写进日志和响应头
   X-Request-ID，出问题时用户报 ID 就能精确定位整条请求的日志

两个接口：
- POST /api/chat        非流式（保留兼容）
- POST /api/chat/stream SSE 流式：Agent 每一步实时推给前端（主要接口）
"""
import os
import json
import time
import uuid
import logging
from collections import defaultdict, deque

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from agent import run_agent, run_agent_stream

load_dotenv()  # 本地运行时读取 .env；线上用平台环境变量

# ---------- 日志配置：统一格式，本地控制台输出，Render 上直接看服务日志 ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("ops-agent.api")

app = FastAPI(title="AI Ops Agent")


@app.middleware("http")
async def request_tracing(request: Request, call_next):
    """请求追踪中间件：每个请求分配唯一 ID，记录方法/路径/状态/耗时。

    排查线上问题的标准姿势：前端把响应头里的 X-Request-ID 报给你，
    你拿 ID 去日志里一搜，这次请求的所有日志就全出来了。
    """
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    start = time.time()
    response = await call_next(request)
    elapsed_ms = round((time.time() - start) * 1000)
    logger.info("%s %s -> %s (%dms) req_id=%s",
                request.method, request.url.path, response.status_code,
                elapsed_ms, request_id)
    response.headers["X-Request-ID"] = request_id
    return response

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
    """非流式聊天接口：POST {"message": "..."} -> Agent 分析 -> {"answer", "steps"}"""
    _check_rate_limit(request.client.host)
    if len(req.message) > MAX_MESSAGE_LEN:
        raise HTTPException(status_code=400, detail=f"消息过长，最大 {MAX_MESSAGE_LEN} 字符")
    try:
        return run_agent(req.message)
    except Exception as e:
        return {"answer": f"服务出错: {e}", "steps": []}


@app.post("/api/chat/stream")
def chat_stream(req: ChatRequest, request: Request):
    """SSE 流式聊天接口：Agent 循环的每个事件实时推给前端。

    SSE（Server-Sent Events）格式：每个事件为
        event: 事件名\\n
        data: JSON\\n\\n
    比 WebSocket 简单：单向推送够用，且浏览器 fetch 原生支持。
    """
    _check_rate_limit(request.client.host)
    if len(req.message) > MAX_MESSAGE_LEN:
        raise HTTPException(status_code=400, detail=f"消息过长，最大 {MAX_MESSAGE_LEN} 字符")

    def event_gen():
        try:
            for event, data in run_agent_stream(req.message):
                yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'answer': f'服务出错: {e}'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/health")
def health():
    """健康检查接口（运维项目给自己加个 health check，面试加分）

    顺带暴露知识库状态，方便确认 RAG 模块是否正常初始化。
    """
    from knowledge_base import count as kb_count
    return {
        "status": "ok",
        "kb_chunks": kb_count(),  # -1 表示知识库不可用（降级运行）
    }


@app.get("/")
def index():
    """首页：直接返回聊天页面（单文件前端，不用 npm 构建）"""
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


if __name__ == "__main__":
    import uvicorn
    # 本地默认 8000；云平台通过 PORT 环境变量注入真实端口
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
