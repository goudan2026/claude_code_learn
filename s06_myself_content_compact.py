from operator import itemgetter
import os
from pathlib import Path
from anthropic import Anthropic
from dotenv import load_dotenv
import subprocess
import json
import time


# 终端着色（便于区分父）；Windows 10+ 默认支持 ANSI
C_RESET = "\033[0m"
C_TRANSCRIPT = "\033[93m"  # 亮黄：存档路径
C_TASK = "\033[35m"  # 洋红：task 派发
C_TOOL = "\033[32m"  # 绿：工具返回摘要
C_REPLY = "\033[96m"  # 亮青：本轮助手最终文本

"""Part1：模型环境变量 & 客户端初始化"""
load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=True)
api_key = os.getenv("ANTHROPIC_API_KEY")
base_url = os.getenv("ANTHROPIC_BASE_URL")
model_id = os.getenv("MODEL_ID")
WORKDIR = Path.cwd()
client = Anthropic(api_key=api_key, base_url=base_url)
SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks."
KEEP_RECENT = 3
PRESERVE_RESULT_TOOLS = {"read_file"}
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
THRESHOLD = 5000

"""Part2：工具函数"""
def safe_path(p:str) -> Path:
    ''''检查路径是否在当前工作目录下，是的话返回Path对象，否则返回None。'''
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径逃逸出工作区:{p}")
    return path

#函数run_bash：执行操作系统相关命令，并返回命令执行结果。
def run_bash(command:str) -> str:
    """执行操作系统相关命令，并返回命令执行结果。"""
    r = subprocess.run(command, shell=True, cwd=WORKDIR, capture_output=True, text=True, timeout=120)
    output = (r.stdout + r.stderr).strip()
    return output[:50000] if output else "(no output)"

#函数run_read：读取文件，并返回文件内容。
def run_read(path:str, limit:int=None) -> str:
    """防止过多行数，或者巨行出现"""
    text = safe_path(path).read_text(encoding='utf-8', errors='ignore')
    line = text.splitlines()
    if limit is not None and limit < len(line):
        #超过了限制行数，就进行截断
        line = line[:limit]
    return "\n".join(line)[:50000]

#函数run_write：写入文件，并返回写入结果。
def run_write(path:str, content:str) -> str:
    """写入文件，并返回写入结果。"""
    current_path = safe_path(path)
    current_path.write_text(content)
    return f"已覆盖写入{len(content)}字节到{path}"

#函数run_edit：编辑文件，并返回编辑结果。只编辑第一次出现的内容
def run_edit(path: str, old_text: str, new_text: str) -> str:
    """编辑文件，并返回编辑结果。只编辑第一次出现的内容"""
    current_path = safe_path(path)
    content = current_path.read_text(encoding='utf-8', errors='ignore')
    if old_text not in content:
        return f"错误：{old_text}不在{path}中，无法编辑"
    new_content = content.replace(old_text, new_text, 1)
    current_path.write_text(new_content, encoding='utf-8', errors='ignore')
    return f"已编辑{path}"

"""Part3：上下自动压缩 & 微压缩"""
def estimate_tokens(messages: list) -> int:
    """估计messages的tokens数量"""
    return len(str(messages)) // 4

def micro_compact(messages: list) -> list:
    """
    1. 复盘历史工具调用信息，生成[(msg_idx, part_idx, part)]形式的工具复盘表tool_results
    2. 复盘block.id和block.name的映射表，从messages里把AI返回的工具调用信息提取出来，生成{block.id: block.name}的映射表。
    3. 遍历messages在KEEP_RECENT之前的所有msg，如果msg.content长度超过阈值，则用占位符替代。
    """
    #生成工具复盘表tool_results
    tool_results = []
    for msg_idx, msg in enumerate(messages):
        if msg["role"] == "user" and isinstance(msg["content"], list):
            for part_idx, part in enumerate(msg["content"]):
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    tool_results.append((msg_idx, part_idx, part))

    #生成block.id和block.name的映射表,{block.id: block.name}
    tool_name_map = {}
    for msg in messages:
        if msg["role"] == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if hasattr(block, "type") and block.type == "tool_use":
                        tool_name = block.name
                        tool_id = block.id
                        tool_name_map[tool_id] = tool_name
    
    #将KEEP_RECENT之前所有的消息进行压缩，如超过100字符，则进行压缩
    to_clear = tool_results[:-KEEP_RECENT]
    for _, _, tool_result in to_clear:
        if not isinstance(tool_result.get("content"), str) or len(tool_result.get("content", "")) < 100:
            continue
        tool_id = tool_result["tool_use_id"]
        tool_name = tool_name_map.get(tool_id, "unknown")
        if tool_name in PRESERVE_RESULT_TOOLS:
            continue
        tool_result["content"] = f"[Previous: used {tool_name}]"
    return messages


def auto_compact(messages: list) -> list:
    #创建一个目录存档所有历史messages，并记录路径
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    transcript_path = TRANSCRIPT_DIR / f"transcript_{time.time()}.jsonl"
    with open(transcript_path, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    print(f"{C_TRANSCRIPT}已存档历史消息到{transcript_path}{C_RESET}")
    
    #将历史会发发给LLM模型，要求其总结摘要
    conversation_text = json.dumps(messages, default=str)
    response = client.messages.create(
        model=model_id,
        system=SYSTEM,
        messages=[{
            "role": "user",
            "content": 
            "总结此次对话以保持连贯性。" 
            "包括：1) 已完成的事项，2) 当前任务状态，3) 关键决策。4) 下一步计划。"
            "要简洁但保留关键细节。\n\n" + conversation_text
        }],
        max_tokens=2000,
    )
    summary = next((b.text for b in response.content if hasattr(b, "text")), "")
    if not summary:
        summary = "未生成摘要"
    #将总结放入一个新message中并返回
    print(f"[自动压缩完成]存档位置：{transcript_path}\n\n 摘要：{summary}")
    return [{"role":"user", "content":f"[对话已压缩。存档位置：{transcript_path}]\n\n{summary}"}]



"""Part4：工具列表 & 工具路由表"""
tools = [
    {"name":"bash", "description": "运行操作系统相关bash命令。",
    "input_schema": {"type": "object", "properties":{
        "command": {"type": "string", "description": "要执行的bash命令，比如: 'ls -l'"}
    },
    "required": ["command"]}
    },
    
    {"name":"read_file", "description": "读取文件内容。",
    "input_schema": {"type": "object", "properties":{
        "path": {"type": "string", "description": "文件路径，比如: 'test.txt'"},
        "limit": {"type": "number", "description": "最大读取行数，比如: 100，如果超过100行，则截断到100行。"}
    },
    "required": ["path", "limit"]}
    },

    {"name":"write_file", "description": "覆盖写文件内容，完全覆盖，不保留原有内容。",
    "input_schema": {"type": "object", "properties":{
        "path": {"type": "string", "description": "文件路径，比如: 'test.txt'"},
        "content": {"type": "string", "description": "要写入的内容，比如: 'Hello, world!'"}
    },
    "required": ["path", "content"]}
    },

    {"name":"edit_file", "description": "修改文件指定内容，只修改原文第一次出现原内容的位置，比如“苹果不是苹果”，将苹果换为梨子，就是“梨子不是苹果”",
    "input_schema": {"type": "object", "properties":{
        "path": {"type": "string", "description": "文件路径，比如: 'test.txt'"},
        "old_text": {"type": "string", "description": "文本中希望被修改的原内容"},
        "new_text": {"type": "string", "description": "目标内容希望被替换的新内容"}
    },
    "required": ["path", "old_text", "new_text"]}
    },

    {
        "name": "compact", 
        "description": "触发手动历史对话压缩，缓解上下文窗口压力",
        "input_schema":{
            "type": "object",
            "properties":{
                "focus":{
                    "type":"string",
                    "description":"摘要中需要保留的内容，如：已完成的事项，当前任务状态，关键决策，下一步计划等"
                }
            },
        }
    },
]

TOOL_HANDLERS = {
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw["limit"]),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "compact": lambda **kw: print(f"{C_TOOL}[触发压缩]{C_RESET} 正在总结历史对话...", flush=True),
}

"""Part5：Agent Loop 主循环实现"""
def agent_loop(messages: list):
    while True:
        print(f"{C_REPLY}[Agent]{C_RESET} 请求模型中…", flush=True)
        #layer1: 每次请求模型之前，进行微压缩。
        print(f"[请求前，开始微压缩.......tokens数量：{estimate_tokens(messages)}]")
        micro_compact(messages)
        print(f"[请求前，微压缩完成。tokens数量：{estimate_tokens(messages)}]")
        #layer2:如果tokens数量超过阈值，则进行自动压缩
        if estimate_tokens(messages) > THRESHOLD:
            print(f"[上下文长度为{estimate_tokens(messages)}，超过阈值{THRESHOLD}，自动压缩触发...]\n")
            messages[:] = auto_compact(messages)
            print(f"[自动压缩完成，tokens数量：{estimate_tokens(messages)}]")

        response = client.messages.create(
            model=model_id,
            system=SYSTEM,
            messages=messages,
            tools=tools,
            max_tokens=2000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        manual_compact = False
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "compact":
                    manual_compact = True
                    output = "Compressing..."
                else:
                    handler = TOOL_HANDLERS.get(block.name)
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"

                print(f"{C_TOOL}[工具{block.name}结果]{C_RESET} {str(output)[:200]}", flush=True)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
        #将results添加到messages中
        messages.append({"role": "user", "content": results})
        #layer3:如果手动压缩触发，则进行手动压缩
        if manual_compact:
            print("[manual compact triggered]")
            messages[:] = auto_compact(messages)



"""Part6：主函数"""
if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms05 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        final_response = history[-1]["content"]
        _txt = "".join(b.text for b in final_response if hasattr(b, "text")) or "(no output)"
        print(f"{C_REPLY}[Agent回复]{C_RESET}\n{_txt}")

        print()