from operator import itemgetter
import os
from pathlib import Path
from anthropic import Anthropic
from dotenv import load_dotenv
import subprocess
import re
import yaml

# 终端着色（便于区分父 Agent / 子 Agent / task / 工具输出）；Windows 10+ 默认支持 ANSI
C_RESET = "\033[0m"
C_PARENT = "\033[93m"  # 亮黄：父 Agent
C_SUB = "\033[95m"  # 亮洋红：子 Agent
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
SKILLS_DIR = WORKDIR / "skills"


"""Part2：Skill加载类   ---- 这一章重点要写的代码，稍后补充
    包含如下函数：
    _load_all: 从markdown文件中加载所有skill
    _parse_frontmatter: 支持_load_all的工作，完成获取meta、body的功能（正则匹配）
    get_discription: 发现当前可用的skills列表 --- discovery层工作
    get_content: 根据skill name获取skill的body --- loading层工作
"""
class SkillLoader:
    def __init__(self, skill_dir:Path):
        self.skill_dir = skill_dir
        self.skills = {}
        self._load_all()
    
    def _load_all(self):
        if not self.skill_dir.exists():
            raise FileNotFoundError(f"Skill目录不存在: {self.skill_dir}")
        for f in sorted(self.skill_dir.rglob("SKILL.md")):
            text = f.read_text(encoding='utf-8', errors='ignore')
            meta, body = self._parse_frontmatter(text)
            name = meta.get("name", f.parent.name)
            self.skills[name] = {"meta": meta, "body": body, "paht": str(f)}

    def _parse_frontmatter(self, text:str):
        """从markdown文本中提取frontmatter部分，返回meta和body。"""
        pattern = r"^---\n(.*?)\n---\n(.*)"
        match = re.match(pattern, text, re.DOTALL)
        if not match:
            return {}, text
        meta = yaml.safe_load(match.group(1))
        body = match.group(2).strip()
        return meta, body

    def get_discription(self) -> str:
        """返回当前所有可用skills的名称和描述。"""
        lines = []
        for name, skill in self.skills.items():
            line = f"-{name}: {skill["meta"].get("description", "(no description)")}"
            lines.append(line)
        return "\n".join(lines)

    def get_content(self, skill_name:str) -> str:
        """根据skill name获取skill的body。"""
        skill = self.skills.get(skill_name)
        if not skill:
            return f"Unknown skill: {skill_name}"
        return f"skill:{skill_name}\n{skill['body']}\n"
        




SKILL_LOADER = SkillLoader(SKILLS_DIR)

"""Part3：系统提示词工程（轻发现，深加载）"""
SYSTEM = f"""
你是一个工作在 {WORKDIR} 的 coding agent。
使用“load_skill”功能来获取专业知识，以便在处理不熟悉的话题时能够得心应手。
可使用skills如下:
{SKILL_LOADER.get_discription()}
"""


"""Part4：工具函数"""
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

"""Part5：工具列表 & 工具路由表"""
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
        "name":"load_skill", 
        "description": "根据你从可用skills中选择的skill，加载获取对应skill的具体内容（body）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "skill_name":{
                    "type": "string",
                    "description": "要加载的skill名称，比如: 'pdf'或'mcp'"
                }
            },
            "required": ["skill_name"]
        }
    },
]

TOOL_HANDLERS = {
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw["limit"]),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "load_skill": lambda **kw: SKILL_LOADER.get_content(kw["skill_name"]),
}

"""Part6：Agent Loop 主循环实现"""
def agent_loop(messages: list):
    while True:
        print(f"{C_PARENT}[Agent]{C_RESET} 请求模型中…", flush=True)
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
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                print(f"{C_TOOL}[工具{block.name}结果]{C_RESET} {str(output)[:200]}", flush=True)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
        #将results添加到messages中
        messages.append({"role": "user", "content": results})

"""Part7：主函数"""
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