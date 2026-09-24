"""
会话记忆存储（SQLite）：Agent 的"记忆力"。

解决的问题：LLM 本身无状态，之前每个请求只有 system + 当前问题，
用户追问"刚才那个域名呢"时模型一脸茫然。现在同一 session_id 的
历史对话会被持久化，并在下一轮注入 Agent 的 messages 上下文。

设计要点（面试可讲）：
1. SQLite 单文件库（backend/ops_memory.db，gitignore），零运维依赖，
   免费云实例够用；生产可平滑换 PostgreSQL，接口不变
2. 只持久化 user / assistant 最终回答，工具调用中间过程不入库——
   排查细节可追溯性由前端步骤浮窗和日志承担，上下文窗口留给有效信息
3. 每条入库截断 1500 字符、加载取最近 8 条：控制上下文长度，
   防止多轮对话把 token 成本和延迟越滚越大（上下文预算管理）
"""
import os
import sqlite3
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ops_memory.db")
HISTORY_LIMIT = 8       # 注入上下文的最大历史消息条数（约 4 轮对话）
MAX_CONTENT_LEN = 1500  # 单条消息入库截断


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        created_at TEXT, updated_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT, role TEXT, content TEXT, created_at TEXT)""")
    return conn


def append_message(session_id: str, role: str, content: str):
    """追加一条消息并更新会话时间戳。content 超长截断。"""
    now = datetime.now().isoformat(timespec="seconds")
    content = (content or "")[:MAX_CONTENT_LEN]
    with _conn() as conn:
        conn.execute(
            "INSERT INTO sessions(session_id, created_at, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET updated_at=excluded.updated_at",
            (session_id, now, now))
        conn.execute(
            "INSERT INTO messages(session_id, role, content, created_at) VALUES(?,?,?,?)",
            (session_id, role, content, now))


def get_history(session_id: str, limit: int = HISTORY_LIMIT) -> list:
    """取某会话最近 limit 条消息（时间正序），返回 OpenAI messages 格式。"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT role, content FROM ("
            "  SELECT id, role, content FROM messages"
            "  WHERE session_id=? ORDER BY id DESC LIMIT ?"
            ") ORDER BY id ASC", (session_id, limit)).fetchall()
    return [{"role": r, "content": c} for r, c in rows]


def clear_session(session_id: str):
    with _conn() as conn:
        conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
