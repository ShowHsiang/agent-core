---
id: S_02_runtime-monitor
title: ReAct 运行进展监控与 BAI 规约
type: S
author: ShowHsiang
author_email: xiangxiaoneng@h-partners.com
created: 2026-11-11
updated: 2026-11-24
modules:
  - openjiuwen/core/single_agent
  - openjiuwen/harness
files:
  - openjiuwen/core/single_agent/runtime_monitor/schema.py
  - openjiuwen/core/single_agent/runtime_monitor/memory.py
  - openjiuwen/core/single_agent/runtime_monitor/evaluator.py
  - openjiuwen/core/single_agent/runtime_monitor/monitor.py
  - openjiuwen/core/single_agent/runtime_monitor/__init__.py
  - openjiuwen/core/single_agent/agents/react_agent.py
  - openjiuwen/harness/deep_agent.py
---

# ReAct 运行进展监控与 BAI 规约

## 1. 范围与事实归属

适用于挂载 RuntimeMonitor 的原生 ReActAgent 和 DeepAgent。执行仍由现有 ReAct、工具执行器和权限链控制；monitor 只消费结果、评估进展并提供请求级建议。BAI 本期限定为有界评估开销下的进展干预，不预测价格、不增加预算门禁、不实现恢复层。

- 工具调用、最终结果、模型 usage 来自执行链；业务验收及工具实际 usage 来自宿主 EvidenceAdapter。
- JEV/LLM 产生语义判断，不拥有已验证事实、完成状态或工具执行权。
- RuntimeMemory 是 Session 中的有界诊断状态；窗口和建议由它派生，不替代 context、permission state 或任务完成条件。
- 每个 monitor 对应一个逻辑 agent。跨 session 隔离；同一 session/monitor 不支持并行驱动多个主循环。

依据：[D-20261029-1613-react-runtime-intervention](../../designs/active/D-20261029-1613-react-runtime-intervention/design.md)。

## 2. 公共接口

导入路径为 `openjiuwen.core.single_agent.runtime_monitor`，公开类型以该包 `__all__` 为准。

| 入口 | 契约 |
| --- | --- |
| `RuntimeMonitor(jev_client=..., config=..., evidence_adapter=..., scope_id=..., on_update=...)` | 默认构造 JevRuntimeEvaluator。客户端须独立设置 `max_retries=0`，其生命周期由宿主管理；缺少 client/evaluator 时 ValueError。 |
| `ReActAgent.set_runtime_monitor(monitor)` / `DeepAgent.set_runtime_monitor(monitor)` | 返回 self。None 关闭；原有 factory/config/Card 无新增必填项。DeepAgent 转发给当前及重建后的内层 ReAct。 |
| `start_task(session, goal, task_id=..., goal_version=..., criteria=...)` | 建立显式业务任务，替换该作用域旧窗口，返回独立快照；随后多次 invoke 保持任务直到再次 start_task。 |
| `get_memory(session)` | 返回 RuntimeMemory 独立副本或 None。不能通过修改返回值更新会话。未知 schema_version 抛 ValueError；ReAct 挂载失败只关闭本次监控。 |
| `await publish(session)` / `on_update(session, memory)` | 可选异步通知，传独立 RuntimeMemory 快照；不新增评估。评估边界及 ReAct/DeepAgent 收尾调用；未注册时空操作。 |
| `RuntimeEvaluator.assess_step(data)` | 异步返回 StepAssessment，包含每条观测等级、候选证据对关系、偏离与建议响应判断。 |
| `RuntimeEvaluator.review_intervention(data)` | 异步返回 InterventionReview，判断必要性并选取代码提供的 option_id。 |
| `LlmRuntimeEvaluator(chat_model)` | 使用 SDK 异步 invoke 接口与严格 JSON 结果，可整体替换或经 review_evaluator 只替换复核阶段。 |
| `EvidenceAdapter.extract(tool_call, result, message)` | 同步转换已发生的结果为 ToolEvidence；不执行检查或网络调用。 |

默认普通 invoke/stream 建立新自动任务；HITL/workflow 中断恢复延续原任务、原 step。DeepAgent 的一个外层任务及其内部续轮共用窗口。显式业务任务优先于自动划分。

## 3. 数据与不变量

1. 一步是一次有效模型回复及其工具批次，完整批次才可评估。并行工具不增加步数；部分完成、暂停和工具重试不重复结算。无工具回复记 terminal；取消后的部分结果记 aborted，仅留诊断，不计零收益。
2. 逐工具回调只记录暂存结果，None 表示旧尝试作废。最终工具结果按 call_id 去重；中断 call 不标完成。纯等待由 adapter 标识，整个批次均为等待时不进入收益窗口。
3. step_id 在任务内单调递增；观测标识包括 step、call_id 与批次内序号。事实、实际 usage、语义判断及建议状态分字段保存，不用模型自报费用替代实际消耗。
4. Verification 必须来自 adapter、匹配注册 criterion_id 与可选版本，并携带 proof_ref。每项首次通过最多贡献一次已验证进展；已通过条件被推翻产生 PREMISE_REFUTED。
5. 同一会话键保存 `{schema_version: 1, generation, revision, payload: JSON}`。JSON 载荷保留字面点号 ID，并避免 Session 递归 merge 留下被删除的验证/冲突。重建 monitor 若要读取同一状态须使用稳定 scope_id。
6. 每次异步评估前复制输入；返回后核对 generation/revision。旧任务或旧版本结果不得回写。新 steering 提升 goal.version，使旧建议过期、旧 step 不再参与新窗口，保留历史事实供比较。
7. 默认保留 32 步、64 条观测、8 条建议、每条建议最近 16 个请求 ID；每步最多 8 条观测，每条文本最多 800 字符。保留限制不等于审计日志完整性保证；所有状态随 Session 生命周期持久化，不另建全局存储。

## 4. 收益及触发算法

对完整且非纯等待的第 t 步，先通过评估 A 判定每条观测，再由代码计算：

- 首次通过可信验收：`g_t = 1`。
- 否则取本轮最大贡献：NEW_EVIDENCE = 0.5，STATE_PROGRESS = 0.25，NO_GAIN = 0；不累加并行工具数量。
- 状态改善必须存在 before/after，且二者不同。缺字段为 UNKNOWN；相同状态为 NO_GAIN。
- 完整精确重复观测不再获得新证据奖励；只有 adapter 标识 repeatable 时才可确定归零，包括后端失败时。通用“OK”回执重复不能证明状态未变化。
- 有正收益但资料不完整时保留收益下界；无正收益且任一观测未知/被裁剪时 `g_t = None`，不是 0。收益下界不得证明低收益。

窗口为最近 N 个 eligible step，N 默认 5。只对已知 g 求和与均值，覆盖率为已知数/窗口步数；另记已验证进展数、连续低收益、连续 UNKNOWN、重复动作数及本窗口首次出现且未解决的冲突数。候选比较覆盖历史观测与当前工具批次内此前的观测，同批每对只枚举一次。所有候选按结构键与文本重合排序，整轮默认最多取 3 对，共用一次评估 A。不同 scope/version 的报告不能构成同时矛盾。每个原始矛盾对单独保留在 `conflicts`。双方都有相同的非空 subject、property、version 及相同 scope 时，共享同一项未解决分歧。缺少归属的证据沿用按对统计。窗口计算本窗口首次发现或重新打开、目前仍未解决的分歧数。

每轮只消费截至该轮的裁决。完整的宿主 Observation 可以用 `resolution_ref` 指定该事实的裁决来源。唯一裁决会关闭归属相同的所有证据对，并留下引用。不同裁决值会将问题重开。缺少结构化值时使用裁决引用识别，两个不同且无法比对的裁决保留分歧。模型建议或证据自称权威不能关闭结构化分歧。裁决命中已送达建议的具体证据目标时可记录 improved。业务验收仍需 Verification。旧 v1 载荷从保留的观测回填可恢复的归属，保留历史证据对 ID。

候选信号为以下条件的并集：

| 信号 | 默认条件 |
| --- | --- |
| LOW_GAIN_STREAK | 前 2 个完整步为宽限，之后连续 3 步完整收益 < 0.25；UNKNOWN/下界中断连续计数 |
| LOW_WINDOW_GAIN | 满 5 步，已知覆盖率 ≥ 0.8，不含收益下界，已知收益均值 < 0.25 |
| REPEATED_ACTION | 窗口中相同动作参数及完整结果指纹出现 ≥ 3 次，且这些步收益为 0/未知 |
| EVIDENCE_CONFLICT | 窗口新出现或重开的独立未解决分歧 ≥ 2 项 |
| PREMISE_REFUTED | 本步可信验收推翻已有验证结论 |
| OFF_TRACK | 评估 A 明确判定偏离 |
| ASSESSMENT_GAP | 连续 3 个 UNKNOWN 步 |

有活动建议时不重复复核；两次复核间默认冷却 2 个评估步。连续无进展片段最多产生 2 条建议，然后标 stalled；出现正进展或新的独立冲突时复位。验收前提被推翻可以越过冷却和活动建议，替换旧建议。

## 5. 两阶段评估与建议闭环

正常步最多一次 A；代码已更新窗口且候选信号通过控制条件后，最多一次 B。JEV 一次 system_one 调用合并同阶段独立问题，B 使用 A 更新后的统计，不把互相依赖的判断塞入同一请求。

A 使用有限 gain/relation choice 和偏离 noul；有真实送达建议时增加是否响应及是否命中目标。choice 要求完整合法分布、有限概率、和近似 1、所选项为最高概率，默认最高概率 ≥ 0.75 且领先 ≥ 0.20。B 的选项门槛为 0.65 / 0.15；noul 采用双阈值保留不确定区。量表/阈值是初始工程值，不构成统计置信保证。

B 只能选代码菜单 CONTINUE / VERIFY / REPLAN；有可信 anchor 时才提供 BACKTRACK。BACKTRACK 只重审推理，绝不删除消息或撤销工具效果。模型返回未知 option_id 不执行任何新动作；不确定且存在客观候选信号时降级为 VERIFY，仅 OFF_TRACK 且不确定时不提示。

建议经当前请求的 system 内容注入，不写共享 prompt builder。证据带来源并标为不可信数据；用户指令优先。只有实际派发给 provider 的 messages 包含 advice_id 和标记才记录 delivered；上下文预览不算送达，连续两次未送达使建议过期。

主模型仍决定下一步计划/工具参数。后续默认两个完整步观察该建议，状态可为 improved、no_observed_improvement 或 inconclusive；改善需命中特定目标并取得收益/可信验证，不能把任意无关进展当成建议有效。自动任务结束时活动建议为 inconclusive；observe 模式只记录 observed_only，完全不注入请求。

## 6. 失败、兼容和限制

- A/B 默认各 1.5 秒截止时间，无默认重试。后端异常/低置信输出变为 UNKNOWN；原工具结果与主流程不受影响。Cancellation 不吞掉。
- fallback_evaluator 为显式选项，每步最多一个备用调用（默认截止 5 秒）。LLM 自报概率不作为 JEV 分布使用。
- 输入默认最多 12000 字符；先删完整旧记录/证据对，再删当前观测，标记不完整，不截断 JSON。仍超限则走降级；不无限扩大上下文。
- 无下一次主模型决策（直接回答、达到 iteration limit、force finish）时不为评估额外调用主模型或 JEV。
- 未安装 monitor 的行为、工具调用参数和请求内容保持原样；无新增全局 callback、事件或第三方依赖。
- 当前不保证进程崩溃后的跨执行器 exactly-once，不接第三方 provider 的内部循环，也不提供团队总监督。实际 JEV 延迟/误判率需要宿主轨迹验证。

## 7. 关联入口与验证定位

- [接入与适配示例](../../../../docs/runtime_monitor.md)。
- [DeepAgent 生命周期接缝](../harness/S_02_deep-agent-architecture.md)：只转交 monitor 与外层任务生命周期。
- `tests/unit_tests/agent/react_agent/test_runtime_monitor.py`：窗口、去重、版本、降级和建议状态。
- `tests/unit_tests/agent/react_agent/test_runtime_monitor_integration.py`：真实 ReAct invoke/stream、HITL/workflow resume、provider 请求和 DeepAgent factory。


## 7. 宿主投影接口

通知默认 `notification_timeout=0.25` 秒，上限 5 秒。普通异常和超时只记录异常类型，不修改监控状态或主执行结果；取消保持传播，但调用收尾仍须释放 DeepAgent 运行标志及完成其拥有的 Session 清理。通知不持有独立后台循环，回调须遵守异步取消并避免同步阻塞。独立快照的修改不能回写 Session；宿主以 generation/advice_id 标识派生通知，不把 publish 当作可靠消息队列或恢复事务。

应用可从同一快照投影诊断和建议生命周期，SDK 不定义产品 WebSocket 事件或聊天组件。jiuwenswarm 的接入和事件契约在其 `docs/runtime-monitor.md`：原生主 Agent 创建/重载装配 monitor，默认 JEV、保留 LLM 后端，复用 Session 任务边界；白名单诊断入既有历史，稳定建议 ID 更新聊天提示。业务验收仍需要独立 EvidenceAdapter，不能从 UI/TODO 完成状态推断。
