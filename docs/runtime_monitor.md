# ReAct Runtime Memory 与 BAI

RuntimeMonitor 为原生 ReActAgent 和 DeepAgent 提供可选的进展反馈。工具批次完成后记录观察，在下一次模型决策前评价收益与证据；需要干预时将建议加入真实模型请求。主 ReAct 模型继续负责计划和工具调用。

## 默认 JEV 接入

已有配置好的 `agent` 和 `session` 时：

```python
from openjiuwen.core.foundation.llm.system_one import JevSystemOneClient
from openjiuwen.core.single_agent.runtime_monitor import RuntimeMonitor, RuntimeMonitorConfig

async with JevSystemOneClient(
    api_key=jev_api_key,  # 由宿主的配置/密钥管理传入
    timeout=1.5,
    max_retries=0,
) as client:
    monitor = RuntimeMonitor(
        jev_client=client,
        config=RuntimeMonitorConfig(window_size=5, mode="active"),
    )
    agent.set_runtime_monitor(monitor)  # 同样支持 DeepAgent
    result = await agent.invoke({"query": "调查并验证问题原因"}, session)
    memory = monitor.get_memory(session)  # 独立数据副本，适合诊断展示

agent.set_runtime_monitor(None)
```

客户端的创建与关闭属于宿主。JEV 客户端必须设置 `max_retries=0`，避免监控请求重试放大延迟；monitor 另外限制每阶段的整体截止时间。未挂载 monitor 时不会新增评估请求、工具参数或提示。

普通任务每次 invoke 新建窗口；HITL/workflow resume 沿用原窗口。DeepAgent 同一个外层任务中的内层 ReAct 调用共用窗口。需要跨多次 invoke 延续同一业务任务时，显式调用：

```python
monitor.start_task(session, "核实版本 v2 的测试结果", task_id="investigation-42", goal_version=1)
```

显式任务直到宿主再次 `start_task()` 才替换。一个 monitor 绑定一个逻辑 agent；不同 session 的数据隔离。重建 monitor 并读取已有会话数据时传入相同 `scope_id`。会话使用版本化 JSON 载荷保存有界状态，不提供崩溃恢复或环境回滚。

## 收益、触发与建议

- 默认最近 5 个完整工具批次为窗口。并行工具算一轮，重试和暂停不增加轮次。
- 首次通过业务验收为 1，有用新证据为 0.5，有依据的状态改善为 0.25，无新增收益为 0；资料不足为 `None`。
- JEV 判断不能增加“已验证进展”计数。仅注册 EvidenceAdapter 提供且与 acceptance criterion/版本匹配的验证结果可以增加该计数。
- 前 2 轮为探索宽限；之后连续 3 轮低于 0.25、窗口均值低、重复动作、两个新增冲突、验收结论被推翻、明显偏离或连续 UNKNOWN，可进入复核。
- 正常轮最多一次 JEV 调用，触发轮再加一次；同阶段的独立问题合并提交。没有下一次模型决策时不为评估追加一轮。
- 建议类型为 CONTINUE、VERIFY、REPLAN、BACKTRACK。BACKTRACK 仅重审推理，不删除消息、还原文件或撤销副作用。
- 两轮后记录是否响应建议及目标改善；连续两次干预无改善则暂停重复提醒，出现新进展/关键证据后重新观察。

UNKNOWN 不补成 0，也不跨过 UNKNOWN 累加连续低收益。精确重复内容不再次获得新证据奖励；相同的“OK”回执不证明外部状态未变化。只有适配器声明 `Observation.repeatable=True` 时，重复结果才可以直接归零。

初始量表和阈值尚需业务轨迹校准。`mode="observe"` 记录评估和建议，但不加入主模型请求，适合先观察误触发情况。

## 业务证据和验收

EvidenceAdapter 是同步提取接口，接收最终工具调用、结果和 ToolMessage，返回 ToolEvidence。它不应发起网络请求；需要运行检查时，先通过正常工具完成检查，再转换已有结果。

```python
from openjiuwen.core.single_agent.runtime_monitor import (
    AcceptanceCriterion, Observation, ToolEvidence, Verification,
)

class TestEvidence:
    def extract(self, tool_call, result, message):
        # 示例：此工具的实际结构化结果由宿主定义，不能信任模型自报通过。
        if tool_call.name != "run_tests":
            return ToolEvidence(observations=[Observation(text=str(message.content))])
        return ToolEvidence(
            observations=[Observation(text=str(message.content), source=result["report_path"])],
            verifications=[Verification(
                criterion_id="tests", version=result["artifact_version"],
                proof_ref=result["report_path"], passed=result["failed"] == 0,
            )],
        )

monitor = RuntimeMonitor(jev_client=client, evidence_adapter=TestEvidence())
monitor.start_task(session, "让 v2 的测试通过", criteria={
    "tests": AcceptanceCriterion(description="测试套件通过", version="v2"),
})
agent.set_runtime_monitor(monitor)
```

适配器还可以提供 `state_before/state_after`、subject/property/scope/version/value 以及已经测得的工具 usage。纯等待操作通过 `ToolEvidence.waiting=True` 标识。默认只从工具报告提取观察，不将任意文件 hash 变化或成功回执解释为目标进展。

## LLM 替换与故障降级

```python
from openjiuwen.core.single_agent.runtime_monitor import LlmRuntimeEvaluator

monitor = RuntimeMonitor(
    evaluator=LlmRuntimeEvaluator(chat_model, timeout=5),
    config=RuntimeMonitorConfig(assessment_timeout=5, review_timeout=5),
)
```

`chat_model` 使用 SDK 的异步 `invoke(messages=..., tools=None, timeout=...)` 接口。LLM 返回严格解析的 JSON 等级、判断和选项 ID；它自报的概率不作为 JEV 概率使用。自定义 evaluator 实现 `assess_step()` 和 `review_intervention()` 两个异步方法即可。

可用 `review_evaluator` 单独替换复核阶段；`fallback_evaluator` 显式开启备用模型，每轮最多调用一次。默认后端失败走规则降级：未知语义记 UNKNOWN，只使用实际验收、可信重复及可计算窗口，必要时给出 VERIFY 建议。取消继续向上传播。

`get_memory(session)` 提供步骤、原始 usage、评估诊断、建议送达与后续结果。实际消耗只有来源提供时才记录，缺失保持未知；本功能不预测成本，也不增加预算或权限门禁。

## 宿主通知与应用接入

需要在运行中展示诊断时，可以传入异步 `on_update(session, memory)`。回调收到独立快照；修改快照不会写回 Session，也不会改变下一步决策。

```python
async def on_progress(session, memory):
    # 宿主自行白名单投影，不向 UI 广播目标全文、工具参数或原始证据。
    await diagnostics_sink.publish({
        "task_id": memory.goal.task_id,
        "generation": memory.generation,
        "window": memory.window(monitor.config).model_dump(mode="json"),
    })

monitor = RuntimeMonitor(jev_client=client, on_update=on_progress)
agent.set_runtime_monitor(monitor)
```

ReAct 在评估边界及调用收尾发布，DeepAgent 在外层任务收尾补发最终状态。也可显式 `await monitor.publish(session)`。它不额外执行 JEV/LLM 评估；未注册回调时直接返回。通知默认限时 0.25 秒，可用 `RuntimeMonitorConfig.notification_timeout` 调整；普通错误/超时只记录异常类型，取消向上传播。回调应短小、支持取消、不做同步阻塞 I/O；通知是尽力投递，宿主按 generation/advice_id 去重并读取 Session 快照补齐诊断，不将它当作事务或可靠消息队列。

jiuwenswarm 的产品适配维护在其仓库 `docs/runtime-monitor.md`：配置与客户端生命周期、`chat.runtime_monitor` 白名单投影、聊天通知/历史回放均由应用负责，SDK 不依赖产品事件协议。
