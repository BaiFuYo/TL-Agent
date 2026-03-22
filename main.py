# main.py
from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Dict, Optional
import httpx
import json
import uuid
import time
import re
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import QINIU_API_KEY, QINIU_AI_API_URL, SYSTEM_PROMPT,PLANNER_PROMPT
from tools import tools, available_functions

app = FastAPI(title="TL-Agent")
app.mount("/static", StaticFiles(directory="static"), name="static")
# ====== 会话管理（内存存储）======
SESSION_STORE: Dict[str, dict] = {}
MAX_HISTORY_LENGTH = 8
SESSION_TIMEOUT = 1800
MAX_TOOL_CALLS = 5


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[Message]
    session_id: Optional[str] = None


def cleanup_expired_sessions():
    now = time.time()

    expired = [
        sid for sid, data in SESSION_STORE.items()
        if now - data["last_active"] > SESSION_TIMEOUT
    ]

    for sid in expired:
        del SESSION_STORE[sid]


# ====== 自动记忆系统 ======

def extract_memory(text: str) -> Dict[str, str]:

    memory = {}

    # 城市
    city_match = re.search(r"(去|到|在)([\u4e00-\u9fa5]{2,10})旅游", text)
    if city_match:
        memory["city"] = city_match.group(2)

    # 预算
    budget_match = re.search(r"(\d+)\s*元", text)
    if budget_match:
        memory["budget"] = budget_match.group(1)

    # 时间
    time_match = re.search(r"(\d+月)", text)
    if time_match:
        memory["travel_time"] = time_match.group(1)

    # 天数
    day_match = re.search(r"(\d+)天", text)
    if day_match:
        memory["days"] = day_match.group(1)

    return memory


def merge_memory(old: Dict, new: Dict):

    for k, v in new.items():
        old[k] = v


def memory_to_prompt(memory: Dict) -> str:

    if not memory:
        return ""

    text = "\n用户旅行信息：\n"

    for k, v in memory.items():
        text += f"{k}: {v}\n"

    return text


# ====== 大模型调用 ======

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

    async with httpx.AsyncClient() as client:

        response = await client.post(
            QINIU_AI_API_URL,
            headers=headers,
            json=data,
            timeout=60.0
        )

        return response.json()
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

async def run_agent(messages: List[dict]) -> str:

    # ===== 第一步：生成计划 =====
    plan = await run_planner(messages)

    messages.append({
        "role": "assistant",
        "content": f"执行计划:\n{plan}"
    })

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

    if session_id in SESSION_STORE:

        history = SESSION_STORE[session_id]["messages"]
        memory = SESSION_STORE[session_id].get("memory", {})

        SESSION_STORE[session_id]["last_active"] = time.time()

    else:

        history = []
        memory = {}

    # ===== 提取新记忆 =====

    for m in request.messages:

        new_mem = extract_memory(m.content)

        merge_memory(memory, new_mem)

    # ===== Memory Prompt =====

    memory_prompt = memory_to_prompt(memory)

    # ===== 构造 messages =====

    messages = [{
        "role": "system",
        "content": SYSTEM_PROMPT + memory_prompt
    }]

    messages.extend(history)

    messages.extend(
        [{"role": m.role, "content": m.content} for m in request.messages]
    )

    # ===== Agent执行 =====

    reply = await run_agent(messages)

    # ===== 更新历史 =====

    new_turn = [
        {"role": m.role, "content": m.content}
        for m in request.messages
    ]

    new_turn.append({
        "role": "assistant",
        "content": reply
    })

    updated_history = (history + new_turn)[-MAX_HISTORY_LENGTH:]

    SESSION_STORE[session_id] = {
        "messages": updated_history,
        "memory": memory,
        "last_active": time.time()
    }

    return {
        "reply": reply,
        "session_id": session_id
    }


@app.get("/")
def root():
    return FileResponse("static/frontend.html")