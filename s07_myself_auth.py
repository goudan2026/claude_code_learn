import json
import os
import re
import subprocess
from fnmatch import fnmatch
from pathlib import Path
from unittest.loader import VALID_MODULE_NAME
from anthropic import Anthropic
from dotenv import load_dotenv


# 终端着色；Windows 10+ 默认支持 ANSI
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
SYSTEM = f"""You are a coding agent at {WORKDIR}. Use tools to solve tasks. The user controls permissions. Some tool calls may be denied."""


#----权限模式----

MODES = ("default", "plan", "auto")

READ_ONLY_TOOLS = {"read_file", "bash_readonly"}
WRITE_TOOLS = {"write_file", "edit_file", "bash"}

class BashSecurityValidator:

    VALIDATORS = [
        ("shell_metachar", r"[;&|`$]"),       # shell metacharacters
        ("sudo", r"\bsudo\b"),                 # privilege escalation
        ("rm_rf", r"\brm\s+(-[a-zA-Z]*)?r"),  # recursive delete
        ("cmd_substitution", r"\$\("),          # command substitution
        ("ifs_injection", r"\bIFS\s*="),        # IFS manipulation
    ]

    def validate(self, command: str) -> list:
        failures = []
        for name, pattern in self.VALIDATORS:
            if re.search(pattern, command):
                failures.append((name, pattern))
        return failures

    def is_safe(self, command: str) -> bool:
        return len(self.validate(command)) == 0

    def describe_failures(self, command: str) -> str:
        failures = self.validate(command)
        if not failures:
            return "No issues detected."
        parts = [f"{name}: {pattern}" for name, pattern in failures]
        return "Severity flags: " + ", ".join(parts)

def is_workspace_trusted(workspace: Path) -> bool:
    ws = workspace or WORKDIR
    trust_marker = ws / ".claude" / ".claud_trusted"
    return trust_marker.exists()


bash_validator = BashSecurityValidator()

# --- permission rules ---
DEFAULT_RULES = [
    {"tool": "bash", "content": "rm -rf /", "behavior": "deny"},
    {"tool": "bash", "content": "sudo *", "behavior": "deny"},
    {"tool": "read_file", "path": "*", "behavior": "allow"},
]

class PermissionManager:
    def __init__(self, mode: str = "default", rules: list = None):
        if mode not in MODES:
            raise ValueError(f"Invalid mode: {mode}. Must be one of: {MODES}")
        self.mode = mode
        self.rules = rules or list(DEFAULT_RULES)
        self.consecutive_denials = 0
        self.max_consecutive_denials = 3

    def _matches(self, rule: dict, tool_name: str, tool_input: dict) -> bool:
        if rule.get("tool") and rule["tool"] != "*":
            if rule["tool"] != tool_name:
                return False
            if "path" in rule and rule["path"] != "*":
                path = tool_input.get("path", "")
                if not fnmatch(path, rule["path"]):
                    return False
            if "content" in rule:
                command = tool_input.get("command", "")
                if not fnmatch(command, rule["content"]):
                    return False
        return True
    
    def check(self, tool_name: str, tool_input: dict) -> dict:
        """
        Returns: {"behavior": "allow"|"deny"|"ask", "reason": str}
        """
        #bash高危命令检查
        if tool_name == "bash":
            command = tool_input.get("content", "")
            failures = bash_validator.validate(command)
            if failures:
                severe = {"sudo", "rm_rf"}
                severe_hits = [f for f in failures if f[0] in severe]
                if severe_hits:
                    desc = bash_validator.describe_failures(command)
                    return {"behavior": "deny", "reason": f"Bash validator: {desc}"}
                desc = bash_validator.describe_failures(command)
                return {"behavior": "ask", "reason": f"Bash validator flagged: {desc}"}
    
        #拒绝规则检查
        for rule in self.rules:
            if rule["behavior"] != "deny":
                continue
            if self._matches(rule, tool_name, tool_input):
                return {"behavior": "deny", "reason": f"Blocked by deny rule: {rule}"}

        #确定模式：default、plan、auto
        if self.mode == "plan":
            # Plan mode: deny all write operations, allow reads
            if tool_name in WRITE_TOOLS:
                return {"behavior": "deny", "reason": "Plan mode: write operations are denied."}
            return {"behavior": "allow", "reason": "Plan mode: read operations are allowed."}

        if self.mode == "auto":
            # Auto mode: auto-allow read-only tools, ask for writes
            if tool_name in READ_ONLY_TOOLS:
                return {"behavior": "allow", "reason": "Auto mode: read-only tool is auto-allowed."}
            pass

        # 检查允许规则
        for rule in self.rules:
            if rule["behavior"] != "allow":
                continue
            if self._matches(rule, tool_name, tool_input):
                self.consecutive_denials = 0
                return {"behavior": "allow", "reason": f"Allowed by allow rule: {rule}"}

        #询问用户
        return {"behavior": "ask", "reason": f"No rule matched for tool: {tool_name}. Ask user for permission."}

    def ask_user(self, tool_name: str, tool_input: dict) -> bool:
        preview = json.dumps(tool_input, ensure_ascii=False)[:200]
        print(f"\n [Permission] {tool_name} called with input: {preview}")
        try:
            answer = input("Allow? (y/n/always): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False

        if answer == "always":
            #增加允许规则
            self.rules.append({"tool": tool_name, "content": "*", "behavior": "allow"})
            self.consecutive_denials = 0
            return True
        
        if answer == "y":
            self.consecutive_denials = 0
            return True
        
        #除了以上情况，剩下的应该都是拒绝
        self.consecutive_denials += 1
        if self.consecutive_denials >= self.max_consecutive_denials:
            print(f"  [{self.consecutive_denials} consecutive denials -- "
                  "consider switching to plan mode]")

        return False

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


def agent_loop(messages: list, perms: PermissionManager):
    while True:
        print(f"{C_BLUE}[Agent]{C_RESET} 请求模型中…", flush=True)
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
            
            #执行工具前，先检查权限
            decision = perms.check(block.name, block.input or {})
            
            if decision["behavior"] == "deny":
                output = f"Permission denied: {decision['reason']}"
                print(f"{C_RED}[DENIED]{block.name}{C_RESET}: {decision['reason']}", flush=True)

            elif decision["behavior"] == "ask":
                if perms.ask_user(block.name, block.input or {}):
                    handler = TOOL_HANDLERS.get(block.name)
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                    print(f"{C_GREEN}[ALLOWED]{block.name}{C_RESET}: {output[:200]}", flush=True)
                else:
                    output = f"Permission denied by user for {block.name}"
                    print(f"{C_RED}[USER DENIED]{block.name}{C_RESET}", flush=True)

            else:
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                print(f"{C_GREEN}[ALLOWED]{block.name}{C_RESET}: {output[:200]}", flush=True)

            results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
        #将results添加到messages中
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("Permission modes: default, plan, auto")
    MODE_INPUT = input("Enter permission mode(default): ").strip().lower() or "default"
    if MODE_INPUT not in MODES:
        mode_input = "default"

    perms = PermissionManager(mode=MODE_INPUT)
    print(f"Permission mode: {MODE_INPUT}")

    history = []
    while True:
        try:
            query = input("\033[36mS07 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        #给一个更改模式的代码  /mode <mode>
        if query.startswith("/mode"):
            parts = query.split()
            if len(parts) == 2 and parts[1] in MODES:
                perms.mode = parts[1]
                print(f"{C_YELLOW}[Permission mode changed to: {parts[1]}{C_RESET}")
            else:
                print(f"{C_RED}[Usage: /mode <{'|'.join(MODES)}>{C_RESET}")
            continue
        
        #给一个查看权限规则的代码  /rules
        if query.startswith("/rules"):
            for i, rule in enumerate(perms.rules):
                print(f" {i}: {rule}")
            continue

        history.append({"role": "user", "content": query})
        agent_loop(history, perms)
        final_response = history[-1]["content"]
        _txt = "".join(b.text for b in final_response if hasattr(b, "text")) or "(no output)"
        print(f"{C_GREEN}[Agent回复]{C_RESET}\n{_txt}")

        print()