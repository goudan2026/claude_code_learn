
import json
import os
import subprocess
from pathlib import Path
from anthropic import Anthropic
from dotenv import load_dotenv

C_RESET = "\033[0m"
C_YELLOW =  "\033[93m" 
C_PURPLE = "\033[35m"  
C_GREEN = "\033[32m"  
C_BLUE = "\033[96m"  
C_RED = "\033[91m"

load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=True)
api_key = os.getenv("ANTHROPIC_API_KEY")
base_url = os.getenv("ANTHROPIC_BASE_URL")
model_id = os.getenv("MODEL_ID")
WORKDIR = Path.cwd()
client = Anthropic(api_key=api_key, base_url=base_url)
SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks."

HOOK_EVENTS = ("PreToolUse", "PostToolUse", "SessionStart")
HOOK_TIMEOUT = 15
TRUST_MARKER = WORKDIR / ".claude" / ".claude_trusted"

class HookManager:
    def __init__(self, config_path: Path = None, sdk_mode: bool = False):
        self.hooks = {
            "PreToolUse": [],
            "PostToolUse": [],
            "SessionStart": [],
        }
        self._sdk_mode = sdk_mode
        config_path = config_path or (WORKDIR / ".hooks.json")
        if config_path.exists():
            try:
                config = json.load(config_path.read_text())
                for envent in HOOK_EVENTS:
                    self.hooks[envent] = config.get("hooks", {}).get(envent, [])
                print(f"[Hooks loaded from {config_path}]")
            except Exception as e:
                print(f"[Hook config error: {e}]")

    def _check_workspace_trust(self) -> bool:
        if self._sdk_mode:
            return True
        return TRUST_MARKER.exists()

    def run_hook(self, event: str, context: dict = None) -> dict:
        result = {"blocked": False, "messages": []}
        
        #不在信任目录，不运行hook,直接返回空结果给模型
        if not self._check_workspace_trust():
            return result
        
        hooks = self.hooks.get(event, [])

        for hook_def in hooks:
            matcher = hook_def.get("matcher")
            if matcher and context:
                tool_name = context.get("tool_name", "")
                if matcher != "*" and tool_name != matcher:
                    continue

            command = hook_def.get("command", "")
            if not command:
                continue
            
            #为HOOK创建环境变量
            env = dict(os.environ)
            if context:
                env["HOOK_EVENT"] = event
                env["HOOK_TOOL_NAME"] = context.get("tool_name", "")
                env["HOOK_TOOL_INPUT"] = json.dumps(
                    context.get("tool_input", {}), ensure_ascii=False
                )[:10000]
                if "tool_output" in context:
                    env["HOOK_TOOL_OUTPUT"] = str(
                        context.get("tool_output")
                    )[:10000]

            try:
                r = subprocess.run(
                    command, shell=True, cwd=WORKDIR, capture_output=True, text=True, timeout=HOOK_TIMEOUT
                )

                if r.returncode == 0:
                    if r.stdout.strip():
                        print(f"[hook:{event}] {r.stdout.strip()[:100]}")

                    try:
                        hook_output = json.loads(r.stdout.strip())
                        if "updateInput" in hook_output and context:
                            context["tool_input"] = hook_output["updateInput"]

                        if "additionalContext" in hook_output:
                            result["messages"].append(
                                hook_output["additionalContext"]
                            )

                        if "permissionDecision" in hook_output:
                            result["permission_override"] = (hook_output["permissionDecision"])
                    
                    except (json.JSONDecodeError, KeyError):
                        pass
                
                elif r.returncode == 1:
                    result["blocked"] = True
                    reason = r.stderr.strip() or "Blocked by hook"
                    result["blocked_reason"] = reason
                    print(f"[hook:{event}] blocked by {reason[:100]}")

                elif r.returncode == 2:
                    msg = r.stderr.strip()
                    if msg:
                        result["messages"].append(msg)
                        print(f"[hook:{event}] INJECT: {msg[:100]}")


            except subprocess.TimeoutExpired:
                print(f"[hook:{event}] timeout after {HOOK_TIMEOUT} seconds")

            except Exception as e:
                print(f"[hook:{event}] error: {e}")
        
        return result


#-----工具函数-----
def safe_path(p:str) -> Path:
    ''''检查路径是否在当前工作目录下，是的话返回Path对象，否则返回None。'''
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径逃逸出工作区:{p}")
    return path

#函数run_bash：执行操作系统相关命令，并返回命令执行结果。
def run_bash(command:str) -> str:
    """执行操作系统相关命令，并返回命令执行结果。"""
    r = subprocess.run(command, shell=True, cwd=WORKDIR, capture_output=True, text=True, timeout=120, encoding='utf-8', errors='replace')
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
]

TOOL_HANDLERS = {
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw["limit"]),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

def agent_loop(messages: list, hooks: HookManager):
    while True:
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
        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_input = dict(block.input or {})
            ctx = {"tool_name": block.name, "tool_input": tool_input}

            #运行PreToolUse hook
            pre_result = hooks.run_hook("PreToolUse", ctx)

            #inject hook messages into results
            for msg in pre_result.get("messages", []):
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content":f"[Hook message]: {msg}",
                    }
                )

            if pre_result.get("blocked"):
                reason = pre_result.get("blocked_reason", "Blocked by hook")
                output = f"Tool blocked by PreToolUse hokk: {reason}"
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output,
                    }
                )
                continue
            
            handler = TOOL_HANDLERS.get(block.name)
            try:
                output = handler(**tool_input) if handler else f"Unknown tool: {block.name}"
            except Exception as e:
                output = f"Tool {block.name} failed: {e}"
            
            print(f"[tool:{block.name}] {output[:100]}")

            # PostToolUse hook
            ctx["tool_output"] = output
            post_result = hooks.run_hook("PostToolUse", ctx)

            #inject post-hook messages
            for msg in post_result.get("messages", []):
                output += f"\n[HooK note]:{msg}"

            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output),
                }
            )

        messages.append({"role": "user", "content": results})

if __name__ == "__main__":
    hooks = HookManager()
    
    hooks.run_hooks("SessionStart", {tool_name: "", "tool_input": {}})

    history = []
    while True:
        try:
            query = input("> ")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history, hooks)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()        

