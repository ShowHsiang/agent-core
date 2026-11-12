# 为已有 ReActAgent 启用进展干预

先按同目录的 ReAct 示例创建主模型、工具、`agent` 和 `session`。再挂载默认 JEV monitor：

```python
from openjiuwen.core.foundation.llm.system_one import JevSystemOneClient
from openjiuwen.core.single_agent.runtime_monitor import RuntimeMonitor, RuntimeMonitorConfig

# jev_api_key 由应用配置传入；不写入会话、日志或 Runtime Memory。
async with JevSystemOneClient(api_key=jev_api_key, max_retries=0, timeout=1.5) as jev:
    monitor = RuntimeMonitor(jev_client=jev, config=RuntimeMonitorConfig(mode="active"))
    agent.set_runtime_monitor(monitor)
    result = await agent.invoke({"query": "调查原因，找到证据后验证结论"}, session)
    memory = monitor.get_memory(session)
    window = memory.window(monitor.config)
    for advice in memory.advice:
        print(advice.option.recommendation, advice.state)

agent.set_runtime_monitor(None)
```

初次接入可用 `mode="observe"` 评估轨迹；切换为 active 后，建议才进入主模型请求。`DeepAgent.set_runtime_monitor()` 使用同一接口。验收适配器、LLM 替换和任务边界见[使用说明](../../docs/runtime_monitor.md)。
