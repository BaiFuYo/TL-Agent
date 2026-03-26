# main.py
from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Dict, Optional
from datetime import datetime
import httpx
import json
import uuid
import time
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import sqlite3
import os
from contextlib import contextmanager

from config import QINIU_API_KEY, QINIU_AI_API_URL, SYSTEM_PROMPT, PLANNER_PROMPT
from tools import tools, available_functions

# 数据库存储路径（放在项目目录下）
DB_PATH = "sessions.db"

app = FastAPI(title="TL-Agent")
app.mount("/static", StaticFiles(directory="static"), name="static")
# 初始化数据库表
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            messages TEXT NOT NULL,
            last_active REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()

# 在应用启动时初始化
init_db()
# ====== 会话管理 ======
MAX_HISTORY_LENGTH = 8
SESSION_TIMEOUT = 1800
MAX_TOOL_CALLS = 100

class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[Message]
    session_id: Optional[str] = None

@contextmanager
def get_db():
    """数据库连接上下文管理器"""
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()

def save_session(session_id: str, messages: list):
    """保存会话到数据库"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO sessions (session_id, messages, last_active) VALUES (?, ?, ?)",
            (session_id, json.dumps(messages, ensure_ascii=False), time.time())
        )
        conn.commit()

def load_session(session_id: str) -> Optional[list]:
    """从数据库加载会话"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT messages FROM sessions WHERE session_id = ?", (session_id,))
        row = cursor.fetchone()
        if row:
            return json.loads(row[0])
    return None

def cleanup_expired_sessions():
    """清理过期会话（保留原逻辑，但操作数据库）"""
    now = time.time()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM sessions WHERE last_active < ?",
            (now - SESSION_TIMEOUT,)
        )
        conn.commit()



# ====== 大模型调用 ======
from httpx import ReadTimeout, ConnectTimeout, RequestError

async def call_deepseek(messages: List[dict]) -> dict:
    headers = {
        "Authorization": f"Bearer {QINIU_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "deepseek/deepseek-v3.2-251201",
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "stream": False
    }
    
    timeout = httpx.Timeout(120.0, connect=10.0, read=100.0)
    
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(QINIU_AI_API_URL, headers=headers, json=data)
            response.raise_for_status()  # 检查 HTTP 状态码
            return response.json()
    except ReadTimeout:
        raise Exception("AI 服务响应超时，请稍后再试")
    except ConnectTimeout:
        raise Exception("无法连接 AI 服务，请检查网络")
    except RequestError as e:
        raise Exception(f"AI 服务请求失败: {str(e)}")
    except Exception as e:
        raise Exception(f"AI 服务异常: {str(e)}")


# ======= Planner function ====
async def run_planner(messages: List[dict]) -> str:
    planner_messages = [
        {"role": "system", "content": PLANNER_PROMPT}
    ]
    planner_messages.extend(messages[-3:])  # 只给最近对话
    resp = await call_deepseek(planner_messages)
    plan = resp["choices"][0]["message"]["content"]
    return plan


# ====== Agent 执行器（ReAct）======
async def run_agent(original_messages: List[dict]) -> str:
    messages = [msg.copy() for msg in original_messages] 
    plan = await run_planner(messages)
    messages.append({"role": "assistant", "content": f"执行计划:\n{plan}"})

    tool_call_count = 0
    while tool_call_count < MAX_TOOL_CALLS:
        resp = await call_deepseek(messages)
        reply_message = resp["choices"][0]["message"]
        messages.append(reply_message)

        # 没有工具调用 -> 结束
        if not reply_message.get("tool_calls"):
            return reply_message.get("content", "")

        tool_responses = []
        for call in reply_message["tool_calls"]:
            func_name = call["function"]["name"]
            args = json.loads(call["function"]["arguments"])

            if func_name not in available_functions:
                tool_responses.append({
                    "tool_call_id": call["id"],
                    "role": "tool",
                    "name": func_name,
                    "content": "Tool not found"
                })
                continue

            try:
                result = await available_functions[func_name](**args)
            except Exception as e:
                result = f"Tool execution error: {str(e)}"

            tool_responses.append({
                "tool_call_id": call["id"],
                "role": "tool",
                "name": func_name,
                "content": result
            })

        messages.extend(tool_responses)
        tool_call_count += 1

    return "任务较复杂，工具调用次数达到上限。"


# ====== 核心聊天接口 ======
@app.post("/chat")
async def chat_endpoint(request: ChatRequest):
    cleanup_expired_sessions()

    session_id = request.session_id or str(uuid.uuid4())

    # 从数据库加载历史
    history = load_session(session_id) or []

    # 👇 获取当前时间并格式化
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 构造完整消息列表
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"当前真实时间是：{current_time}"}
        ]
    messages.extend(history)
    messages.extend([{"role": m.role, "content": m.content} for m in request.messages])

    # Agent 执行
    reply = await run_agent(messages)

    # 更新会话历史并保存到磁盘
    new_turn = [{"role": m.role, "content": m.content} for m in request.messages]
    new_turn.append({"role": "assistant", "content": reply})
    updated_history = (history + new_turn)[-MAX_HISTORY_LENGTH:]

    save_session(session_id, updated_history)  # 👈 关键：保存到磁盘！

    return {
        "reply": reply,
        "session_id": session_id
    }
@app.get("/")
def root():
    return FileResponse("static/frontend.html")

from fastapi.responses import JSONResponse

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    error_msg = str(exc)
    print(f"Server error: {error_msg}")  # 打印到 app.log
    return JSONResponse(
        status_code=500,
        content={"error": "服务暂时不可用", "detail": error_msg}
    )