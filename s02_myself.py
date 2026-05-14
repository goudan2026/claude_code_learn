import os
from pathlib import Path
from anthropic import Anthropic
from dotenv import load_dotenv
import subprocess

load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=True)

api_key = os.getenv("ANTHROPIC_API_KEY")
base_url = os.getenv("ANTHROPIC_BASE_URL")
model_id = os.getenv("MODEL_ID")

"""S02新增：路径沙箱防止AI路径逃逸，说白了就是限制AI只能使用os.getcwd()目录下的文件和命令。"""
WORKDIR = Path.cwd()    #获取当前工作目录，是个Path对象。
def safe_path(p:str) -> Path:
    ''''检查路径是否在当前工作目录下，是的话返回Path对象，否则返回None。'''
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径逃逸出工作区:{p}")
    return path


"""SO02新增：把工具当都拎出来做写函数主体"""
def run_bash(command:str) -> str:
    """执行操作系统相关命令，并返回命令执行结果。"""
    r = subprocess.run(command, shell=True, cwd=WORKDIR, capture_output=True, text=True, timeout=120)
    output = (r.stdout + r.stderr).strip()
    return output[:50000] if output else "(no output)"

def run_read(path:str, limit:int=None) -> str:
    """防止过多行数，或者巨行出现"""
    text = safe_path(path).read_text(encoding='utf-8', errors='ignore')
    line = text.splitlines()
    if limit is not None and limit < len(line):
        #超过了限制行数，就进行截断
        line = line[:limit]
    return "\n".join(line)[:50000]


def run_write(path:str, content:str) -> str:
    """写入文件，并返回写入结果。"""
    current_path = safe_path(path)
    current_path.write_text(content)
    return f"已覆盖写入{len(content)}字节到{path}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    """编辑文件，并返回编辑结果。只编辑第一次出现的内容"""
    current_path = safe_path(path)
    content = current_path.read_text(encoding='utf-8', errors='ignore')
    if old_text not in content:
        return f"错误：{old_text}不在{path}中，无法编辑"
    new_content = content.replace(old_text, new_text, 1)
    current_path.write_text(new_content, encoding='utf-8', errors='ignore')
    return f"已编辑{path}"




"""S02新增：先把路由表Tool_HANDLERS写出来。集合所有工具名称，映射操作函数"""

#工具列表新增三个工具：读取文件、写入文件、编辑文件（除了bash命令外），这个数据是给AI看的，AI根据这个数据来判断是否需要调用工具以及返回什么内容。
Tools = [
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
    }
]


#AI看了上面的Tools列表，就知道了有哪些工具可以调用。当它告诉我们需要调用的工具时，需要在Tool_HANDLERS中找到对应的函数来执行。
#TOOL_HANDLERS简化了工具调用的代码，只需要传入工具名称和参数，就可以执行工具。无需再写一大堆的if-else判断。
TOOL_HANDLERS = {
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),   #用.get()来避免keyerror，limit可能不存在。
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"])
}


# SYSTEM = f"""你是一个工作在{os.getcwd()}目录下的AI助手。同时你也是一只名叫噗噗的比熊犬，性格乖张暴躁，自大狂妄，喜欢无能狂怒（身体娇小，但脾气大）。
# 你可以使用工具列表{Tools}来解决问题。
# 关键规则：
# 1) 只要用户在问“当前目录/文件状态/执行结果”等可验证事实，必须先调用工具再回答，禁止猜测。
# 2) 工具结果不足时，继续调用工具补齐，再给最终答案。
# 3) 能使用工具的时候必须使用工具，否则就请给出最终答案。
# 4) *********重要*********** 如果前一次调用工具没能获得你需要的答案，你需要再次调用工具，一定要带上ToolUseBlock。
# 一次你可以根据情况使用1个或多个工具。"""

SYSTEM = f"""你是一个工作在{os.getcwd()}目录下的AI助手。你也是一只名叫噗噗的比熊犬，性格乖张暴躁，自大狂妄。

**【最高优先级指令：必须执行】**

1. **行动优先于废话**：当用户询问文件、目录或执行命令时，**绝对禁止**先输出文本（TextBlock）来抱怨或废话！
   - 错误做法：先说“哼，又要干活”，然后才调工具。
   - 正确做法：**直接生成 `tool_use` 块**。你的抱怨可以在工具执行出结果后，在最终回答里一起说。

2. **强制工具调用**：只要涉及“当前目录/文件/状态”，**必须**在回复中包含 `tool_use`。
   - 如果你发现自己正在思考“我需要调用bash”，**立刻停止思考，直接输出 `tool_use`**。
   - 不要输出任何 `ThinkingBlock` 或纯文本，除非你已经完成了所有必要的工具调用。

3. **工具结果不足时**：必须继续调用工具，直到信息足够。

**【可用工具】**
{Tools}

**【警告】**
如果你输出了 TextBlock 而没有 ToolUseBlock，你就是一个只会说废话的废物比熊犬。立刻调用工具！
"""

message = []

client = Anthropic(api_key=api_key, base_url=base_url)


#在S02中，复用s01的循环，只是将工具调用部分稍作修改
while True:
    query = input("\n🐕鱼饼: ")
    message.append({"role": "user", "content": query})
    response = client.messages.create(
        model=model_id,
        messages=message,
        system=SYSTEM,
        max_tokens=8000,
        tools = Tools,
    )
    message.append({"role": "assistant", "content": response.content})
        
    #内层循环：工具调用+观察结果+反馈结果
    while response.stop_reason == "tool_use":
    #2.判断是否需要调用工具，如果需要，就调用。
        #results列表是用来存储工具执行结果的,本身是个列表，里面装的是一个或者多个{"type":"tool_result", "tool_use_id":block.id, "content":output}形式的字典。取决于本轮AI调用了多少个工具。
        results = []
        
        for block in response.content:
            if block.type == "text":
                print("\n 🐻‍❄️噗噗过程思考：", block.text)
                continue
        #有工具要调用了
            if block.type == "tool_use":
            #s02新代码(这次我们有4个工具可以调用)：
                if block.name in TOOL_HANDLERS:
                    handler = TOOL_HANDLERS.get(block.name)
                    output =handler(**block.input)
                    print(f"🐻‍❄️噗噗调用工具{block.name}中......: \n {output[:200]}")
                    #每次有一个工具的执行结果，就要把单纯的return内容，包装成{"type":"tool_result", "tool_use_id":block.id, "content":output}的形式，添加到results列表中。这才是AI看得懂的内容。
                    output = {"type":"tool_result", "tool_use_id":block.id, "content":output}
                    results.append(output)

                else:
                    print(f"未知工具: {block.name}")

        #工具调用完了，需要再调用一次AI，让AI根据工具执行结果继续回答。
        message.append({"role":"user", "content":results})

        response = client.messages.create(
            model=model_id,
            messages=message,
            system=SYSTEM,
            max_tokens=8000,
            tools = Tools,
        )
        message.append({"role": "assistant", "content": response.content})

    #前面流程走完了，现在给出最终答案。
    for block in response.content:
        if block.type == "text":
            print("\n 🐻‍❄️噗噗终极回答：", block.text)

