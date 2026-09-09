"""
FastAPI 后端入口：Agent 的"门面"。
职责很简单：接收前端的 HTTP 请求 -> 交给 Agent 处理 -> 返回 JSON。
"""
import os
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from agent import run_agent

load_dotenv()  # 读取 .env 里的 API Key

app = FastAPI(title="AI Ops Agent")


class ChatRequest(BaseModel):
    message: str  # Pydantic 自动校验请求体格式


@app.post("/api/chat")
def chat(req: ChatRequest):
    """前端聊天接口：POST {"message": "..."} -> Agent 分析 -> {"answer", "steps"}"""
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
    uvicorn.run(app, host="127.0.0.1", port=8000)
