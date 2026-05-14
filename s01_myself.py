import os
from pathlib import Path
from anthropic import Anthropic
from dotenv import load_dotenv
import subprocess

load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=True)

api_key = os.getenv("ANTHROPIC_API_KEY")
base_url = os.getenv("ANTHROPIC_BASE_URL")
model_id = os.getenv("MODEL_ID")

SYSTEM = f"你是一个工作在{os.getcwd()}目录下的AI助手，请使用bash命令来解决问题。不需要解释，直接给出命令。"
message = [
    {"system": "system", "content": SYSTEM}
]

client = Anthropic(api_key=api_key, base_url=base_url)

#工具列表
tools_list = [
    {
        "name": "run_command",
        "description": "执行操作系统相关命令",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的命令,比如: 'ls -l'"
                }
            },
            "required": ["command"]
        }
    }
]

while True:
    input_text = input("\n请输入\n: ")
    message.append({"role": "user", "content": input_text})
    response = client.messages.create(
        model=model_id,
        messages=message,
        system=SYSTEM,
        max_tokens=8000,
        tools = tools_list
    )
    #内层循环：工具调用+观察结果+反馈结果
    while response.stop_reason == "tool_use":
    #1.将响应内容添加到消息列表中
        message.append({"role": "assistant", "content": response.content})
    #2.判断是否需要调用工具，如果需要，就调用。
        results = []
        for block in response.content:
        #有工具要调用了
            if block.type == "tool_use":
            #如果是我们本次用的这个唯一的工具
                if block.name == "run_command":
                    command = block.input["command"]
                    result = subprocess.run(command, shell=True, capture_output=True, text=True,timeout=30)
                    #subprocess.run的返回值是一个CompletedProcess对象，我们需要获取它的stdout和stderr,成功stderr为空，失败stdout为空。
                    output = result.stdout + result.stderr   
                    #output就是需要更新到message列表中的内容。
                    print(f"----------工具{block.name}执行结果----------------:\n{output}\n")
                    #output也需要更新到message列表中，这样AI才能看到工具执行结果。
                    results.append({"type":"tool_result", "tool_use_id":block.id, "content":output})
        #工具调用完了，需要再调用一次AI，让AI根据工具执行结果继续回答。
        message.append({"role":"user", "content":results})
        response = client.messages.create(
            model=model_id,
            messages=message,
            system=SYSTEM,
            max_tokens=8000,
            tools = tools_list
        )
    #所有工具调用完了，给出最终答案。
    print("AI: ", response.content[0].text)

