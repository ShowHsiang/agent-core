---
id: F_20261111_1938_react-runtime-bai
title: ReAct Runtime Memory 与 JEV 优先 BAI 最小闭环
type: F
author: ShowHsiang
author_email: xiangxiaoneng@h-partners.com
created: 2026-11-11
updated: 2026-11-12
modules:
  - openjiuwen/core/single_agent
  - openjiuwen/harness
  - jiuwenswarm/jiuwenswarm/server/runtime
  - jiuwenswarm/jiuwenswarm/channels/web/frontend
files:
  - openjiuwen/core/single_agent/runtime_monitor/schema.py
  - openjiuwen/core/single_agent/runtime_monitor/memory.py
  - openjiuwen/core/single_agent/runtime_monitor/evaluator.py
  - openjiuwen/core/single_agent/runtime_monitor/monitor.py
  - openjiuwen/core/single_agent/runtime_monitor/__init__.py
  - openjiuwen/core/single_agent/agents/react_agent.py
  - openjiuwen/harness/deep_agent.py
  - tests/unit_tests/agent/react_agent/test_runtime_monitor.py
  - tests/unit_tests/agent/react_agent/test_runtime_monitor_integration.py
  - jiuwenswarm/pyproject.toml
  - jiuwenswarm/scripts/use-local-agent-core.ps1
  - jiuwenswarm/jiuwenswarm/resources/config.yaml
  - jiuwenswarm/jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/jiuwenswarm/server/runtime/agent_adapter/runtime_monitor.py
  - jiuwenswarm/jiuwenswarm/server/runtime/session/session_history.py
  - jiuwenswarm/jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts
  - jiuwenswarm/jiuwenswarm/channels/web/frontend/src/features/historyRestore.ts
  - jiuwenswarm/jiuwenswarm/channels/web/frontend/src/features/runtimeMonitorNotices.ts
  - jiuwenswarm/jiuwenswarm/channels/web/frontend/src/utils/wsEventDedup.ts
  - jiuwenswarm/tests/unit_tests/agentserver/test_runtime_monitor.py
  - jiuwenswarm/jiuwenswarm/channels/web/frontend/tests/runtimeMonitorNotices.test.mjs
designs:
  - D-20261029-1613-react-runtime-intervention
---

# ReAct Runtime Memory 与 JEV 优先 BAI 实施归档

## 元信息与关联依据

- 归档日期：2026-11-11（Asia/Singapore）。
- 开发范围：`codex/react-runtime-bai` 的工作区实现、单测及文档；按用户请求新建分支，没有提交或推送。
- 设计：[D-20261029-1613-react-runtime-intervention](../designs/active/D-20261029-1613-react-runtime-intervention/design.md)。用户已确认 SDK 最小闭环范围；设计继续保持 active/in-development，等待交付确认，不伪造评审人。
- 当前契约：[S_02_runtime-monitor](../specs/core_runtime/S_02_runtime-monitor.md)、[Harness S_01](../specs/harness/S_01_public-api-and-construction.md)、[Harness S_02](../specs/harness/S_02_deep-agent-architecture.md)。
- 使用入口：[SDK 使用说明](../../../docs/runtime_monitor.md)、[ReAct 接入示例](../../../examples/react_agent/runtime_monitor.md)。

## 背景与决策归属

理论架构收缩为原生 ReAct 执行反馈、Runtime Memory、BAI 建议及后续效果观察。默认用 JEV 回答有限的语义判断，保留 LLM/自定义 evaluator 替换接口；主模型继续生成实际行动。本记录是该设计的第一份实现记录，不替代其它历史特性的结论。

本次没有成本预测、预算门禁、检查点、恢复/补偿、第三方 provider 内部循环、团队统一监督或 jiuwenswarm 展示接入。用户关于“回退”的本期含义是重新审视有证据支持的推理起点。

## 实际实现与状态

新增 `core/single_agent/runtime_monitor` 四个职责文件及导出，复用现有 Session 和工具执行路径：

1. ReAct 有效模型响应建立 StepRecord，逐工具回调暂存部分事实，最终批次按 call_id 结算。HITL/workflow resume 继续原 step，取消只保留 partial facts。
2. 下一次模型决策前评估 A：JEV 合并收益、证据关系、偏离及建议响应等独立问题。代码过滤非法观测 ID、去重并更新最近 N 轮统计。
3. 代码候选触发经过活动建议、冷却和干预次数限制，再调用 B 复核及选取有限建议。每轮默认一至两次 JEV 请求，备用 evaluator 显式启用且每轮最多一次。
4. 建议作为请求级 system 内容注入真实 provider 调用，不改共享 prompt builder；用 advice_id/request_id 确认送达。后续完整步骤记录 improved/no_observed_improvement/inconclusive，不把“已注入提示”当成闭环完成。
5. DeepAgent 转发装配，外层任务开始/结束管理同一窗口，重建内层 ReAct 仍继承 monitor。没有新 factory 参数或顶层 harness 导出。

状态分别维护：步骤 partial/closed/terminal/aborted；建议 created/delivered/observing 和效果终态。目标更换、steering、取消及过期结果处理详见当前 spec，避免在实施记录重复定义全部阈值。

## 实际决策与拒绝的方案

- **代码负责验证，JEV 负责语义。** 工具成功、输出变长或模型宣称完成不能算已验证进展。只有带 criterion/version/proof_ref 的可信 adapter 验收计入该字段；没有业务 adapter 时仍能使用语义收益，但 verified 计数保持零。
- **按完整批次算收益最大值。** 并行调用不能通过堆叠工具数量提高收益，UNKNOWN 不折算零；部分正信息作为下界且不得触发低收益。重复“OK”不证明状态没变化，只有 adapter 明确 repeatable 的观测支持规则归零。
- **两个阶段有真实依赖。** B 必须看到 A 更新后的窗口；拒绝在一个 JEV 请求中让独立问题读取尚未生成的另一题答案。选择选项与是否有必要可以同批独立判断。
- **接入执行链而非仅 after-tool rail。** 后者无法覆盖重试、取消、HITL 恢复和 no-tool 结束。逐工具 callback 仅保留暂存事实，不在回调里调用模型。
- **显式可选装配。** 默认未挂 monitor 的调用签名/模型请求保持不变；专用 JEV client 禁止自动重试，避免进展监控放大主循环等待。
- **建议不拥有控制权。** 不建立第二个 agent，不创造工具执行指令，不用低收益直接终止任务或撤销副作用。有限菜单可由 LLM 替换评估，具体动作仍走现有主模型和权限链。

## 与设计的实现差异

- Session 实际 `update_state` 会递归 merge 字典并解释点号路径。直接写嵌套 RuntimeMemory 字典会保留已删除的旧验证，并破坏带点 criterion_id。实现用版本化 envelope + JSON payload 原子替换逻辑载荷，保留原 Session 存储机制；真实 Session 回归覆盖了该问题。
- 同时使用随机 generation 和递增 revision，防止新任务重置 revision 后旧异步结果误匹配；不是增加分布式事务层。
- 仅保留有界步骤/观测/诊断；不构造独立 append-only 审计服务。工具成本仅接收 adapter 已有 usage，缺少来源保持未知。
- 开发自检补齐：可信重复在后端失败时仍可得零收益；收益下界不能累计低收益；实际送达请求标识保留最近 16 个，避免纯等待期间无限积累。

这些选择落实了原设计边界，没有新增需要产品决策的成本或恢复能力。

## 实际执行与证据

最终候选为本分支 2026-11-11 的未提交工作区。Windows PowerShell，仓库 `.venv`，`PYTHONPATH` 包含仓库根；Python 字节码写入关闭。模型/JEV 使用确定性桩，没有真实付费请求。

初始化后执行：

```powershell
python -m pytest tests/unit_tests/agent/react_agent/test_runtime_monitor.py tests/unit_tests/agent/react_agent/test_runtime_monitor_integration.py tests/unit_tests/agent/react_agent/test_react_agent_tool_fact_retention.py tests/unit_tests/agent/react_agent/test_react_agent_iteration_limit.py tests/unit_tests/agent/react_agent/test_react_agent_resume_continuation.py tests/unit_tests/core/foundation/llm/test_jev_system_one_client.py tests/unit_tests/harness/test_deep_agent.py tests/unit_tests/harness/test_deep_agent_rail_event_routing.py tests/unit_tests/agent/react_agent/test_react_agent_streaming.py tests/unit_tests/agent/react_agent/test_react_agent_cancel_context.py tests/unit_tests/agent/react_agent/test_react_agent_empty_response.py tests/unit_tests/agent/react_agent/test_react_agent_interrupt.py tests/unit_tests/agent/react_agent/test_react_agent_mock.py tests/unit_tests/agent/react_agent/test_new_react_agent_mock.py -q -o addopts='' --disable-warnings
```

结果：**295 passed，19 warnings，13.43 秒**。其中新监控单测及集成测试 39 项；其余为相关既有回归。未执行全仓测试、格式化或静态检查（遵循仓库禁止项）。

| 层次 | 观察到的行为 | 证据边界 |
| --- | --- | --- |
| 窗口与规则 | 宽限、连续低收益、未知、下界、重复、冲突去重/版本、可信验收反证、冷却与连续无改善停提示 | 直接检查记录/计数与独立预期，不证明业务量表准确率 |
| 评估接口 | 默认 JEV 两阶段批次、非法概率/低置信降级、超时/取消、至多一次 fallback、LLM JSON 替换 | 确定性 typed/client 桩；非真实 JEV 服务 |
| 真实 ReAct 入口 | invoke、模型流式、公开 stream 的最终 provider messages 含对应建议；第一请求和关闭监控对照不含；共享 prompt builder 未污染 | 真实主循环/Session，模型与工具结果可控 |
| 中断与结束 | 真实 ConfirmInterruptRail 暂停前工具执行数 0，批准后为 1；同一批次只评价一次；workflow resume、取消、force finish、iteration limit 不增加错误评价 | HITL 用真实 AbilityManager/Rail 和测试工具；不依赖生成器自述执行情况 |
| DeepAgent | 通过 create_deep_agent 构造并 invoke，最终模型请求收到提示；重建转发及 None 关闭有效 | 原生单轮 factory 入口，未验证所有宿主/第三方 provider |
| 生命周期/数据 | steering 到达评估期间使旧建议过期；新任务丢弃旧 await 结果；真实 Session 保留点号 ID 并删除失效字段；输入与记录上限 | 当前进程会话行为，不保证崩溃恢复 exactly-once |

开发中曾发现关闭 monitor 时对 MagicMock ctx.extra 的错误判断改变了 executor 参数；现已增加真实 dict 边界检查，关闭监控的无新增参数回归及工具事实保留测试通过。首次失败记录不作为最终候选通过证据。

## 检视与资料同步

已盘点并追通 ReAct 最终请求、工具结算/恢复、DeepAgent 外层生命周期、JEV typed API 和 Session 存储边界。按 jw-review 进行范围内自检，发现的收益边界已返回开发修正并重验。新增核心 spec，更新 Harness S_01/S_02，补齐 SDK 文档与示例；索引由 sync_documents 生成，不手工维护。

验证支持 SDK 最小闭环交付；没有把仓内测试外推为 jiuwenswarm 上线验证。若再改变最终请求装配、resume 路径、Session merge、评估协议或阈值，须重跑相应行为证据。

## 2026-11-11 交接时遗留

| 项目 | 性质与当前边界 | 后续验证条件 |
| --- | --- | --- |
| JEV 量表与性能 | 初始工程阈值；未测真实请求延迟、业务误触发率/漏报率 | 宿主提供代表性轨迹，先 observe，再比较人工标签与实际延迟 |
| 下游应用 | jiuwenswarm 装配/展示未在本次修改 | 下游挂载 monitor、传入标准化业务证据并核对最终模型请求 |
| 领域验收 | 无 adapter 时不能证明业务目标达成 | 业务实现 criterion/version/proof_ref 提取并独立验证 |
| 持久恢复 | Session 保存有界诊断；无恢复层/跨进程 exactly-once 保证 | 独立恢复层方案；不能把当前 BACKTRACK 当状态还原 |
| 交付 | 已在新分支准备可审查工作区，无提交/推送 | 用户需要提交时按 harness 规则分别组织实现、测试、文档提交 |


## 2026-11-12：jiuwenswarm 第一阶段接入

本次续写同一特性，落实用户要求的应用接入、新分支及本地 SDK 联调。替代上节“下游应用未接入”的交接状态；不覆盖 2026-11-11 的 SDK 验证事实。成本预测从后续路线移除，恢复层等待他人实现后对接。

### 候选与实施范围

- SDK 工作区：`codex/react-runtime-bai`，基线 `8cfbd87896d5f55f1bf15c269c428b260639d036`，包含前一阶段未提交实现。
- 应用工作区：`codex/runtime-bai-adaptation`，由 `cherry-electron-browser` 的 `6e84fb2c4e7f6645d4a78187f0263bcb6af08b90` 新建；原有未跟踪 UI 调试日志和搜索目录未改动。
- 两仓均未提交、推送。应用 `.venv` 实际导入 `D:/agent-core/openjiuwen/core/single_agent/runtime_monitor/monitor.py`；脚本和测试均使用这个 editable SDK。

应用的 RuntimeMonitorBinding 是装配与投影层，没有复制收益/触发算法。默认关闭，开启后默认 observe/JEV；active 才注入主模型请求。create/reload 配置没变时复用 client，切换模式/后端刷新评分 generation，关闭/cleanup 释放独立 JEV client。LLM 模式借用当前 chat model，不关闭共享模型；无效 reload 保留旧配置。

SDK 新增有界 `on_update`/`publish`，ReAct 在评估及收尾发布、DeepAgent 在外层收尾补最终状态。应用把快照投影为 `chat.runtime_monitor`，把建议状态投影为已有 `chat.notice`。会话历史保存诊断及通知；WebSocket 去重按建议及状态区分，前端用稳定 notice_id 更新同一条提示，回放保留最后状态。不会广播原始目标、工具参数/证据或密钥。

本次检查发现两个消费者缺口并已在开发阶段修复：原 WebSocket 只按 request_id 去重，会吞掉同一请求的效果更新；原 history 空文本过滤会丢弃结构化快照。已补对应传输去重与真实文件写入测试。

### 双仓联调决策与限制

`pyproject.toml` 的 uv source 使用 `../agent-core` editable；移除主依赖/harmony 中旧远端 SHA。`scripts/use-local-agent-core.ps1` 先核对 SDK 分支，再以 `--no-deps --no-build-isolation` 安装到已有应用 venv，并验证导入路径。已实际执行成功。启动使用该 venv 或 `uv run --no-sync`。

完整 `uv lock` 因新 SDK 的 OpenTelemetry 下限与应用 A2UI/Google ADK/`google-genai<2` 组合冲突而失败；原 lock 未修改，没有伪造新的完整锁。未为此强制升级 GenAI/ADK 或覆盖冲突约束。当前联调复用已有依赖，仅替换 SDK；干净环境、普通 uv sync 和生产打包尚未闭环。这是依赖集成限制，不是已完成的全量版本锁定。

### 实际验证（Windows / Python venv）

- SDK 本次候选：runtime monitor 单测/集成、DeepAgent、rail routing、interaction 共 **159 passed**。通知副本隔离、最终状态、通知超时/异常、取消后的主循环清理得到覆盖。前阶段 295 项是其当时的范围，本次没有将其重复计入当前测试总数。
- 应用专项：**14 passed**。真实 adapter factory、create_deep_agent、invoke 和 run_one_round 入口；可控 JEV/模型/工具替身。active 实际执行 search→verify，observe 执行 search→search；核对下一次模型消息、建议末状态、客户端资源、双会话标识、重载、发送失败重试、实际 history 文件。
- 应用扩展回归：运行上述专项、session_history_empty_filter、agent_reload_scope、a2x_client_init、deep_adapter_runtime_config_timing、context_engine_config_wiring、permission_cold_build，结果 **117 passed，2 failed**。两项失败都是 cold_build 的 project_dir/workspace_dir 大小写断言；在相同本地 SDK/环境下，仅将 interface_deep 在进程内替换为应用 HEAD 原始源码，仍得到相同 **2 failed，1 passed，10 deselected**。未修改这个既有 Windows 路径行为，也不宣称全套通过。
- 前端：新通知/历史专项 **4 passed**，既有 subagent history **20 passed**，WebSocket event dedup **5 passed**。只用 esbuild 转译加 Node test，没有运行 tsc 或其它静态检查。旧 dedup 测试的转译产物放在忽略目录，并加临时 package.json 声明 ESM。
- 本地脚本实际安装 `openjiuwen 0.1.20` 并核对分支/导入路径；没有执行真实 JEV/LLM 请求，没有完整浏览器 E2E，未验证所有 provider/subagent/生产依赖安装。

### 后续与重验条件

应用第一阶段已接上监控→触发→主模型建议→效果→用户提示/历史。业务 criterion/version/proof_ref 适配尚待扩展；没有可信 adapter 时“已验证进展”保持 0。后续先补主要业务工具证据，再用真实轨迹校准 observe，逐步打开 active；恢复层单独集成，成本预测不再安排。

产品使用和事件协议见 jiuwenswarm 的 `docs/runtime-monitor.md`；SDK 通知契约同步到 S_02_runtime-monitor 与 Harness S_02。若修改 request/stream 传输、history 空记录过滤、reload/task 边界、SDK 评分或更换依赖，需要重新执行相应行为验证。当前工作区仍待用户验收，不归档设计为已完成。


### 收尾检视与修复复验

按 jw-review 只读核对了本次 SDK 通知增量及应用配置/客户端生命周期、原生 factory/interaction、事件解析、history 过滤/文件落盘、前端去重/回放、editable 来源和使用文档。检视发现新异步收尾通知的取消会跳过 DeepAgent 运行标志清理；先补测试实际复现 `_invoke_active=True`，返回 jw-dev 以 finally 保证清理。对应 invoke、stream、interaction_round 三条路径及 ReAct 自有 Session 的 close/commit 均新增取消回归。修复后 SDK 159 项与应用专项 14 项再次通过；前端代码未受此次清理修改影响，沿用本轮已通过的 29 项证据。

对最终差异再次只读复查，通知取消仍向调用方传播，清理不会被通知跳过；默认未启用监控时不新增请求。没有新增未处理的范围内实现问题；完整依赖锁、两项既有 Windows 路径断言、领域验收和真实 JEV/浏览器验证保持上述限制。该结论不是全仓审计、生产验收或全依赖兼容性保证。


### 2026-11-12：按用户指示改名并交付至 fork

用户明确要求将 SDK 分支改为 `react-runtime-bai`、应用分支改为 `runtime-bai-adaptation`，并推送同名分支到 fork。本节更新当前交付安排；此前带 `codex/` 的分支名与未提交状态保留为当时的实施记录。

SDK 使用配置的 `origin`（`https://gitcode.com/m056838700/agent-core.git`），应用使用配置的 `origin`（`https://gitcode.com/lzzzzzzasd/jiuwenswarm.git`）。仅将本次特性的实现、测试和文档纳入提交，保留原有无关工作区文件；不向 upstream 推送。应用联调脚本的分支校验及使用说明同步使用新名称，editable 目录不变。

SDK 按实现、测试、文档组织三个连续提交，应用提交适配及联调材料。推送使用普通同名分支推送，不改写远端历史；推送是否成功以 Git 远端校验为准。本次仅修改分支引用及联调说明，前节行为测试证据仍适用，完整依赖锁限制保持不变。

用户在提交准备期间明确回复“本次不关联 issue”，因此本次提交不添加 `Refs: #...` footer；未修改仓库的通用提交规则。


## 2026-11-13：官方 develop 基线同步与共享 JEV 配置

用户进一步确认两个分支需要集成各自官方最新 develop，应用 JEV 必须复用浏览器 Agent / 前端“决策模型”配置。SDK `react-runtime-bai` 已通过合并提交 `ac1007990` 纳入官方 `fbe6a0d903522712de8094bffd1017964280bdae`；合并冲突仅在生成索引，通过 sync_documents 重建。原有 SDK 实现、测试提交保持在历史中，没有改写或强推。

基线更新后的 SDK 相关回归：runtime monitor 单测与 ReAct 集成、JEV System One 客户端、DeepAgent、rail routing、task completion 共 **207 passed**（Windows / Python venv）。没有运行格式化或静态检查，也未调用真实模型服务。

应用 `runtime-bai-adaptation` 纳入官方 `f5638897d03df426c82fd3a9e9417b59cde7f504`（先合入 `4b7a8d3f9` 后再纳入其更新），仍以 editable 导入本 SDK。应用通过现有 resolve_jev_connection 复用 browser.decision；没有增加 SDK 配置 API。应用侧配置迁移、凭据生命周期、测试及依赖限制的事实维护在 [应用 F](../../../../jiuwenswarm/.agents/agent-docs/features/F_20261113_1109_runtime-bai-shared-jev.md) 和该仓 S_01_runtime-monitor，不在此重复维护。

完整依赖锁仍未解决：最新应用要求 OpenTelemetry SDK <1.42，本 SDK 要求 >=1.45.1。已验证的 editable 联调不代表干净环境安装或生产打包兼容。成本预测继续排除，恢复层等待独立实现集成。
