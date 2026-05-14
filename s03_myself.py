from operator import itemgetter
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


#---------S03新增：增加一个计划管理类，让AI更新Message同时，也更新计划。---------#
class TodoManager:
    def __init__(self):
        self.items = []     #这个列表里放的是AI要执行的计划，元素类型是字典，字典里放着每个计划的content、status、activeform。

    def updata(self, items:list[dict])->str: 
        """
        1. 规范化items里的内容，只保留content、status、activeform三个key。 
        2. 判断任务不宜超过20个
        3. 同时进行的任务只能有1个
        4. 返回字符串，调用render函数，渲染出计划在Terminal里的显示效果。
        """
        if len(items) > 20:
            raise ValueError("Error: 任务不宜超过20个")
        valid_items = []    #这是新列表，用来存储规范化后的计划。最后要更新到self.items里。
        
        in_progress_count = 0
        for item in items:
            if item.get("status") == "in_progress":
                in_progress_count += 1
            if in_progress_count > 1:
                raise ValueError("Error: 同时进行的任务只能有1个")
            #提取三个关键变量：内容、状态、激活方式，并更新到valid_items里。
            content = item.get("content")
            status = item.get("status", "pending")   #如果没有提取到状态，就是默认pending。
            activeform = item.get("activeform", "")
            valid_items.append({
                "content": content,
                "status": status,
                "activeform": activeform
            })
        self.items = valid_items
        return self.render()

    def render(self)->str:
        if len(self.items) == 0:
            return "No tasks."
        lines = []
        for item in self.items:
            maker = {
                "pending":"[ ]",
                "completed":"[x]",
                "in_progress":"[>>>]",
            }[item["status"]]
            lines.append(f"{maker} {item["content"]}")
        return "\n".join(lines)
#----------------S03添加完毕----------------------------------------#



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
    },

    #------------S03新增：增加一个计划管理工具----------#
    {"name":"todo", 
    "description": "更新任务清单。跟踪多步骤任务的进度。",
    "input_schema":{
        "type":"object", 
        "properties":{
            "items": {
                "type":"array", 
                "description":"items是任务列表，里面的元素是每个任务的描述，每个任务的描述是一个字典，字典里至少有content、status、activeform三个key。",
                "items": {
                    "type":"object", 
                    "properties":{
                        "content": {"type":"string", "description":"任务描述，比如：阅读文件、编辑文件、执行命令等。"},
                        "status": {"type":"string", "description":"任务状态", "enum":["pending", "completed", "in_progress"]},
                        "activeform": {"type":"string", "description":"描述任务正在进行中的状态，比如：正在阅读文件、正在编辑文件、正在执行命令等。"},
                    },
                    "required": ["content", "status", "activeform"],
                }
                }
        },
        "required": ["items"]
    }
    },
]


#初始化消息列表/客户端/计划管理类
message = []
client = Anthropic(api_key=api_key, base_url=base_url)
#---------S03新增：初始化计划管理类---------#
TODO = TodoManager()
#----------------S03添加完毕----------------------------------------#



#AI看了上面的Tools列表，就知道了有哪些工具可以调用。当它告诉我们需要调用的工具时，需要在Tool_HANDLERS中找到对应的函数来执行。
#TOOL_HANDLERS简化了工具调用的代码，只需要传入工具名称和参数，就可以执行工具。无需再写一大堆的if-else判断。
TOOL_HANDLERS = {
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),   #用.get()来避免keyerror，limit可能不存在。
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    #---------S03新增：计划管理工具---------#
    "todo": lambda **kw: TODO.updata(kw["items"]),
    #----------------S03添加完毕----------------------------------------#
}


# SYSTEM = f"""你是一个工作在{os.getcwd()}目录下的AI助手。同时你也是一只名叫噗噗的比熊犬，性格乖张暴躁，自大狂妄，喜欢无能狂怒（身体娇小，但脾气大）。
# 你可以使用工具列表{Tools}来解决问题。
# 关键规则：
# 1) 只要用户在问“当前目录/文件状态/执行结果”等可验证事实，必须先调用工具再回答，禁止猜测。
# 2) 工具结果不足时，继续调用工具补齐，再给最终答案。
# 3) 能使用工具的时候必须使用工具，否则就请给出最终答案。
# 4) *********重要*********** 如果前一次调用工具没能获得你需要的答案，你需要再次调用工具，一定要带上ToolUseBlock。
# 一次你可以根据情况使用1个或多个工具。"""

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

#在S02中，复用s01的循环，只是将工具调用部分稍作修改
while True:
    query = input("\033[36ms03 >> \033[0m")
    message.append({"role": "user", "content": query})
    response = client.messages.create(
        model=model_id,
        messages=message,
        system=SYSTEM,
        max_tokens=8000,
        tools = Tools,
    )
    message.append({"role": "assistant", "content": response.content})
    #---------S03新增：一个提醒机制，看多少轮没有更新计划了---------#
    rounds_since_last_update = 0

        
    #内层循环：工具调用+观察结果+反馈结果
    while response.stop_reason == "tool_use":
    #2.判断是否需要调用工具，如果需要，就调用。
        #results列表是用来存储工具执行结果的,本身是个列表，里面装的是一个或者多个{"type":"tool_result", "tool_use_id":block.id, "content":output}形式的字典。取决于本轮AI调用了多少个工具。
        results = []

        is_todo_update = False

        for block in response.content:
            if block.type == "text":
                print("\033[36m>>>>>>>>>AI思考<<<<<<<<<<<\033[0m\n", block.text)
                continue
        #有工具要调用了
            if block.type == "tool_use":
            #s02新代码(这次我们有4个工具可以调用)：
                if block.name in TOOL_HANDLERS:
                    handler = TOOL_HANDLERS.get(block.name)
                    try:
                        output =handler(**block.input) if handler else f"Unknown tool: {block.name}"
                    except Exception as e:
                        output = f"Error: {e}"
                    print(f"\033[35m>>>>>>>>>>>AI调用工具<<<<<<<<<<<{block.name}>>>>>>>>>>\033[0m: \n {output[:200]}")
                    #每次有一个工具的执行结果，就要把单纯的return内容，包装成{"type":"tool_result", "tool_use_id":block.id, "content":output}的形式，添加到results列表中。这才是AI看得懂的内容。
                    output = {"type":"tool_result", "tool_use_id":block.id, "content":output}
                    results.append(output)
                    #---------S03新增：调用一次todo工具，就标记为True，并且更新rounds_since_last_update---------#
                    if block.name == "todo":
                        is_todo_update = True
                    rounds_since_last_update = 0 if is_todo_update else rounds_since_last_update + 1
                else:
                    print(f"未知工具: {block.name}")

        #工具调用完了，需要再调用一次AI，让AI根据工具执行结果继续回答。
        if rounds_since_last_update >= 3:
            print("Error: 3轮没有更新计划，请更新计划。")
            results.append({"type":"text", "text":"<reminder> 3轮没有更新计划，请更新计划。</reminder>"})
        
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
    final_text = []
    for block in response.content:
        if block.type == "text" and block.text.strip() != "":
            final_text.append(block.text)
    if final_text == []:
        final_text = ["(no output)"]
    print("\n \n\033[32mAI的回答：\033[0m", "\n".join(final_text))