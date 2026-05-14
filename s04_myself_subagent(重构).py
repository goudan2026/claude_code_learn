from operator import itemgetter
import os
from pathlib import Path
from anthropic import Anthropic
from dotenv import load_dotenv
import subprocess

# 终端着色（便于区分父 Agent / 子 Agent / task / 工具输出）；Windows 10+ 默认支持 ANSI
C_RESET = "\033[0m"
C_PARENT = "\033[93m"  # 亮黄：父 Agent
C_SUB = "\033[95m"  # 亮洋红：子 Agent
C_TASK = "\033[35m"  # 洋红：task 派发
C_TOOL = "\033[32m"  # 绿：工具返回摘要
C_REPLY = "\033[96m"  # 亮青：本轮助手最终文本

"""Part1：模型环境变量 & 系统提示词"""
load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=True)
api_key = os.getenv("ANTHROPIC_API_KEY")
base_url = os.getenv("ANTHROPIC_BASE_URL")
model_id = os.getenv("MODEL_ID")
WORKDIR = Path.cwd()

client = Anthropic(api_key=api_key, base_url=base_url)
SYSTEM = f"""你是一个工作在 {WORKDIR} 的 coding agent。

规则（必须遵守）：
1) 只要问题属于“可验证事实”（目录、文件、命令结果、环境状态、代码内容），在给出最终答案前必须先调用工具。
2) 如果工具结果报错、为空、或证据不足，必须继续调用工具，不得直接结束回答。
3) 在同一轮用户问题中，最多允许调用 5 次工具进行尝试。
4) 若在 5 次工具调用后仍无法得到足够证据，停止继续调用工具，并给出简洁、礼貌、明确的失败说明：说明你尝试过、当前缺失什么信息、下一步建议用户提供什么。

输出要求：
- 优先工具，后结论。
- 结论只基于工具结果，不猜测。

环境判定规则（强制）：
1) 只允许根据工具输出判断系统，不允许猜测。
2) 若命令失败，不得推断“你是 macOS/Linux/Windows”；必须原样报告错误并继续重试等价命令。
3) 在本会话中，工作目录固定为 D:\agent。查询目录只用 Windows 命令：cd、dir。
4) 若出现“系统找不到指定路径”或“不是内部或外部命令”，必须立即再次调用 bash 重试，不得直接给最终文字结论。
5) 连续失败最多 5 次；超过 5 次时才允许输出“暂时无法完成”，并附上最后一次错误原文。
"""

# 子 Agent 专用：尽量短，降低每次请求的 system token；父 Agent 仍用 SYSTEM
SYSTEM_child = f"""你是子任务 coding agent，工作目录 {WORKDIR}。
工具：bash / read_file / write_file / edit_file；路径相对于工作区根目录。
涉及目录、文件内容、命令结果等可核查事实时，先调用工具再下结论；结论只依据工具输出，禁止臆测。
当前环境为 Windows：列目录用 dir；读文件内容优先 read_file；不要用仅 Unix 常见的命令（如 head）；必要时用 type、Python 一次性读写。
命令报错时原样引用错误，可换等价 Windows 命令少量重试；回复简练。"""

# Part2：工具的具体实现

#函数safe_path：检查路径是否在当前工作目录下，是的话返回Path对象，否则返回None。
WORKDIR = Path.cwd()
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


"""Part3：工具列表 & 工具路由表"""

#子工具列表
child_tools = [
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
]

#父工具列表
parent_tools = child_tools + [
    {
        "name": "task",
        "description": "扩展一个拥有干净上下文的子Agent。与父Agent共享文件系统但是不共享对话历史" ,
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "交给子Agent的可执行说明（越具体越好）。程序会自动追加轮次上限说明，无需你在正文重复。",
                },
                "description": {
                    "type": "string",
                    "description": "子任务的详细描述",
                },
            },
            "required": ["prompt"]
            }
        }
]

#路由表
TOOL_HANDLERS = {
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw["limit"]),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

"""Part4：子Agent的实现"""
#函数 run_subagent：运行子Agent，并返回子Agent的执行结果, 维持一个纯净的submessage列表。
def run_subagent(prompt:str)->str:
    sub_messages = [{"role": "user", "content": prompt}]
    for i in range(30):
        print(f"{C_SUB}[子 Agent {i+1}/30]{C_RESET} 请求模型中…", flush=True)
        response = client.messages.create(
            model=model_id,
            system=SYSTEM_child,
            messages=sub_messages,
            tools=child_tools,
            max_tokens=2000,
        )
        sub_messages.append({"role": "assistant", "content": response.content})
        #如果子Agent没有调用工具，则结束循环
        if response.stop_reason != "tool_use":
            break
        #如果子Agent调用工具，则执行工具
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else "(no output)"
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
                print(f"{C_TOOL}[子Agent工具结果]{C_RESET} {str(output)[:200]}", flush=True)
        sub_messages.append({"role": "user", "content": results})
    #循环超过30次或子Agent没有调用工具，则结束循环
    #将最后一次返回的response.content里的所有text拼接起来，返回给父Agent
    final_answer = "".join(b.text for b in response.content if hasattr(b, "text")) or "(no output)"
    print(f"{C_TOOL}[子Agent最终答案]{C_RESET} {final_answer[:200]}", flush=True)
    return final_answer


"""Part5：父Agent的实现"""
#一个简单的loop循环
def agent_loop(messages: list):
    while True:
        print(f"{C_PARENT}[父 Agent]{C_RESET} 请求模型中…", flush=True)
        response = client.messages.create(
            model=model_id,
            system=SYSTEM,
            messages=messages,
            tools=parent_tools,
            max_tokens=2000,
        )
        messages.append({"role": "assistant", "content": response.content})
        #如果父Agent没有调用工具，则结束循环
        if response.stop_reason != "tool_use":
            return
        #如果父Agent调用工具，则执行工具。除了task工具，其他工具通过handler执行。
        results = []
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "task":
                    desc = block.input.get("description")
                    prompt = block.input.get("prompt")
                    print(f"{C_TASK}[task]{C_RESET} ({desc}) : {prompt[:80]}", flush=True)
                    output = run_subagent(prompt)
                else:
                    handler = TOOL_HANDLERS.get(block.name)
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                print(f"{C_TOOL}[工具结果]{C_RESET} {str(output)[:200]}", flush=True)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
        #将results添加到messages中
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms04 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        final_response = history[-1]["content"]
        _txt = "".join(b.text for b in final_response if hasattr(b, "text")) or "(no output)"
        print(f"{C_REPLY}[父 Agent 回复]{C_RESET}\n{_txt}")

        print()