# 三方 Agent Harness 接入开发指南

本文面向希望让自有 Python Agent、CLI Agent SDK 或 Jiuwen 后续 SDK 作为
OpenJiuwen Team 成员运行的开发者，说明如何实现
`openjiuwen.agent_teams.external.protocol` 2.1。

> 当前阶段只发布协议定义。现有 Claude Code 与 Codex backend 尚未迁移，team spawn
> 路径也尚未通过 provider registry 自动加载三方实现。三方项目可以先实现和测试协议；
> registry、`MemberRuntime` adapter 与声明式配置接线将在后续版本提供。

## 1. 接入层级

本协议定义完整 Harness 行为，而不是一次模型调用：

```text
OpenJiuwen Team
      |
MemberRuntime adapter（后续提供）
      |
ExternalHarnessProtocol
      |
三方 Agent Harness
      |
模型 SDK / CLI / 远端 Agent 服务
```

适合直接实现协议的对象包括：

- 维护长期 conversation/session 的 Python Agent SDK；
- 支持多轮 query/receive、steer 和 interrupt 的 coding-agent SDK；
- 对远端 Agent 服务做连接管理的 Python client；
- 自己拥有 Turn 队列和状态机的 CLI wrapper。

如果三方 SDK 只提供一次性 `run(prompt)`，实现方仍需在 Harness 内补齐消息队列、
Turn 生命周期、事件转换、provider interaction、取消和 checkpoint 语义。

### 1.1 统一术语

```text
Session
└── Turn          一次外部输入 -> 一次稳定外部输出
    └── Iteration 一次 Agent Loop 控制循环
        └── Step  一次可观测原子执行动作
```

`Round` 只表示 multi-agent 协作或协议阶段，一个 Round 可以包含多个 Agent Turn。本接入协议属于
单 Agent Harness 边界，因此统一使用 `turn_id`、`TurnLifecycleEvent`、`TurnEventKind` 和
`turn_events()`。不要把一次 Agent Loop 循环称为 step；应使用 iteration，step 留给 model/tool/
memory/middleware 等原子执行动作。

## 2. 固定导入入口

只从公共包导入，不依赖 `external.cli_agent` 或 protocol 私有模块：

```python
from openjiuwen.agent_teams.external.protocol import (
    AbortMode,
    CheckpointReason,
    DeliveryMode,
    ExternalHarnessCard,
    ExternalHarnessContext,
    ExternalHarnessInput,
    ExternalHarnessProtocol,
    ExternalHarnessProtocolError,
    ExternalHarnessProvider,
    HarnessCapability,
    HarnessCheckpoint,
    HarnessEvent,
    OutputEvent,
    OutputKind,
    SendReceipt,
    ToolApprovalRequest,
    ToolInvocation,
    TurnResult,
    TurnEventKind,
    TurnLifecycleEvent,
    TurnStatus,
    TurnUsage,
    UnsupportedHarnessCapabilityError,
)
```

`@runtime_checkable` 只能检查成员是否存在，不能验证签名、事件次序、状态机、并发安全或
checkpoint 可序列化；生产 provider 必须另跑行为契约测试。

## 3. 生命周期和状态机

每个 Harness 实例只代表一个 team member：

```text
构造实例
   |
start(context)
   |
IDLE --send--> RUNNING --turn terminal--> IDLE
   |                    |
   |                    +--pause--> PAUSED --resume--> RUNNING
   |
stop()
   |
TERMINATED + events EOF
```

约束：

- `start()` 返回时必须可接收输入，状态为 `IDLE`；
- `send()` 只确认消息已接受，不等待模型完成；
- public command 允许从不同协程并发调用；
- Harness 内只有一个逻辑 state writer，例如 supervisor task；
- `stop()` 幂等，并使 `events()`/未完成的 `turn_events()` 最终结束；
- 每个 Turn 只有一个 `STARTED` 和一个 terminal Turn event；
- 未声明能力抛 `UnsupportedHarnessCapabilityError`。

### DeliveryMode

| 模式 | 语义 |
|---|---|
| `AUTO` | IDLE 时启动新 Turn；RUNNING 时排为 follow-up |
| `STEER` | 注入当前 Turn；要求 `HarnessCapability.STEER` |
| `FOLLOW_UP` | 当前 Turn 完成后启动后继 Turn |

`FORCE` abort 只要求尽快停止，不承诺回滚已经发生的命令、文件或外部系统副作用。

## 4. 区分 events、interactions 和 hooks

三者方向和阻塞语义不同：

```text
events:       Harness -> consumer
              单向观测，不返回执行决策

interactions: Harness -> host handler -> response -> Harness
              SDK 主动请求，等待审批/输入/执行结果

hooks:        Harness -> lifecycle hook -> policy result -> Harness
              OpenJiuwen 生命周期策略，可阻断或修改执行
```

Claude 的消息流、Codex 的 turn notification stream 都由 provider adapter 内部消费；adapter 将
观测消息发布到协议 observation channel，将 server/control requests 转给 interactions。Hook
callback 则映射到 hooks。不要把三条通道合并成一个双向 event queue。

## 5. Event 信封和 TurnResult

Observation channel 提供两个消费视图：

| 方法 | 边界 | 适用场景 |
|---|---|---|
| `events()` | 从调用开始持续到 `stop`；跨越所有 Turn | MemberRuntime adapter、后台持续消费 |
| `turn_events()` | 下一个 Turn STARTED 到该 Turn terminal event（均包含） | 串行 query/response、单 Turn 测试 |

这与 Claude SDK 的 `receive_messages()` 和 `receive_response()` 分工一致。两者不是两份消息，也不是
两个订阅：它们消费同一个逻辑单消费者流，禁止并发迭代。简单调用方可以逐轮使用：

```python
await harness.send(ExternalHarnessInput("first task"))
first_turn = [event async for event in harness.turn_events()]

await harness.send(ExternalHarnessInput("follow-up"))
second_turn = [event async for event in harness.turn_events()]
```

`turn_events()` 忽略下一个 Turn STARTED 之前的 observation；找到 STARTED 后按全局顺序产出所有
事件，并在相同 `turn_id` 的 terminal event 产出后立即结束。Terminal event 不能被吞掉，因为
调用方需要从中读取完整 `TurnResult`。Cycle 在 STARTED 前关闭时可空结束；STARTED 后没有 terminal
就关闭必须抛 `ExternalHarnessProtocolError`。

实现必须为 observation channel 加 consumer lease；第二个 active iterator 立即抛
`ExternalHarnessStateError`，不能让两个 async generator 竞争同一个 queue。Iterator 正常结束或
被 `aclose()` 后释放 lease，后续调用才能继续消费。

复杂 team runtime 应只启动一个长期 `events()` consumer，并自行按 `turn_id` 聚合，不应同时调用
`turn_events()`。每条 `HarnessEvent` 的 `sequence` 必须跨所有载荷类型严格递增；`timestamp`
使用 Unix seconds。session、turn、item、消息关联信息放在信封：

```python
event = HarnessEvent(
    sequence=next_sequence(),
    timestamp=time.time(),
    session_id=provider_session_id,
    turn_id=turn_id,
    correlation_id=accepted_message_id,
    event=OutputEvent(
        kind=OutputKind.TEXT,
        content="partial answer",
        is_delta=True,
    ),
)
await event_queue.put(event)
```

公共载荷包括：

| 载荷 | 用途 |
|---|---|
| `OutputEvent` | TEXT、REASONING、STRUCTURED 内容或 delta |
| `ItemLifecycleEvent` | tool call、command、file change 等 provider item |
| `UsageUpdatedEvent` | 标准化 token usage |
| `StateChangedEvent` | Harness state 转换 |
| `TurnLifecycleEvent` | Turn start 和唯一 terminal |
| `HookObservedEvent` | hook 执行观测，不参与授权 |
| `DiagnosticEvent` | 已脱敏诊断 |
| `ProviderEvent` | 带命名空间和版本的 provider JSON 扩展 |

不要先转成 OpenJiuwen 内部 `OutputSchema`。未来 `MemberRuntime` adapter 才负责该转换，协议入口
应保留 SDK item、reasoning、usage 和扩展信息。

Terminal Turn 必须携带结构化结果，并保证事件与状态匹配：

```python
result = TurnResult(
    status=TurnStatus.COMPLETED,
    final_output="final answer",
    structured_output={"answer": "final answer"},
    usage=TurnUsage(input_tokens=120, output_tokens=48, total_tokens=168),
    duration_ms=1320,
    provider_data={"provider_stop_reason": "end_turn"},
)
payload = TurnLifecycleEvent(kind=TurnEventKind.FINISHED, result=result)
```

对应关系是 FINISHED/COMPLETED、ABORTED/INTERRUPTED、PAUSED/PAUSED、FAILED/FAILED。失败结果必须
携带 `TurnError`；不要把原始 exception、client object 或可能包含 credential 的响应塞进事件。

## 6. Provider-initiated interactions

Provider SDK 需要宿主即时回答时，调用 `context.interactions.handle(request)`：

```python
request = ToolApprovalRequest(
    request_id=sdk_request.id,
    call_id=sdk_request.call_id,
    tool_name=sdk_request.tool_name,
    arguments=sdk_request.arguments,
    session_id=self.session_id,
    turn_id=self._active_turn_id,
    provider_data={"provider_method": sdk_request.method},
)
response = await self._context.interactions.handle(request)
if response.request_id != request.request_id:
    raise ExternalHarnessProtocolError("interaction response id mismatch")
```

共享交互类型：

- `ToolApprovalRequest`：工具、命令、文件变更等执行授权；
- `UserInputRequest`：provider 在 Turn 内追问用户；
- `McpElicitationRequest`：MCP server 请求结构化输入；
- `DynamicToolCallRequest`：provider 请求 host 执行运行期工具；
- `ProviderInteractionRequest`：带 provider、request type 和 schema version 的 namespaced JSON 请求。

每个响应必须复制 request 的 `request_id`。当 SDK 发出 cancel、Turn abort 或连接关闭时，adapter
对仍待处理的请求调用 `await context.interactions.cancel(request_id)`；cancel 必须可重复。

如果 adapter 声明 `HOST_INTERACTIONS`，但运行场景没有 handler，它必须采取安全且明确的行为：
在 `start` 拒绝必需交互的配置，或把偶发请求明确映射为 deny/decline。禁止默认 allow，也禁止把
请求发成 event 后无限等待。

### interactions 与 tools 的关系

`ExternalToolGateway` 是 host 预先向 provider 暴露工具的执行入口；`DynamicToolCallRequest` 是
provider 在 active SDK control protocol 中反向委托 host 的请求。实现可以让两者最终使用同一
team tool policy，但不能跳过权限和成员可见性规则。

## 7. Hooks

`HarnessHookDispatcher` 提供 before-prompt、before-tool、after-tool 和 on-stop 生命周期策略。
例如 adapter 真正执行 team tool 前 await `before_tool`，使用 `ToolDecision` 拒绝或改写参数。

`HookObservedEvent` 只能表示 hook 开始或结束。对 Claude Agent SDK 一类同时提供 callback hooks
和 hook event message 的 SDK：callback 映射到 dispatcher，event message 映射到 observation。

Provider approval 和 before-tool hook 可以串联：前者回答 SDK control request，后者执行
OpenJiuwen 统一策略。二者不要用同一个未经区分的回调类型。

## 8. Context、工具和 MCP

`ExternalHarnessContext` 由 host 在 `start()` 时提供：

- team/member/session 身份和 system prompt；
- cwd 和环境变量；
- resume policy、versioned checkpoint 和 checkpoint sink；
- native tool gateway 和 MCP server 配置；
- hook dispatcher、interaction handler 与 telemetry handle。

如果三方 SDK 接受 Python tool callback，使用 `context.tools`：

```python
definitions = await context.tools.definitions()


async def execute_tool(call_id: str, name: str, arguments: dict):
    return await context.tools.invoke(
        ToolInvocation(call_id=call_id, name=name, arguments=arguments)
    )
```

如果三方 Agent 使用 MCP，从 `context.mcp_servers` 读取配置，按 `McpTransport` 转成厂商 SDK
options。不要把 Claude/Codex/Jiuwen 的配置对象写回公共模型。

## 9. Checkpoint 和恢复

Checkpoint 是完整、版本化的 provider envelope：

```python
def _current_checkpoint(self) -> HarnessCheckpoint:
    return HarnessCheckpoint(
        provider="acme-code-agent",
        schema_version="2",
        member_agent_id=self._context.member_agent_id,
        session_id=self._session_id,
        revision=self._provider_revision,
        data={"conversation_id": self._conversation_id},
    )
```

在可恢复状态发生变化时主动保存：

```python
async def _publish_checkpoint(self, reason: CheckpointReason) -> None:
    checkpoint = self._current_checkpoint()
    self._latest_checkpoint = checkpoint
    if self._context.checkpoint_sink is not None:
        await self._context.checkpoint_sink.save(checkpoint, reason=reason)
```

最少在以下时机考虑 save：获得 session/thread id、turn 完成、provider revision 变化、周期刷新和
provider 发出 checkpoint 通知。`save` 返回表示宿主持久化完成；实现需决定失败时重试还是让 Turn
失败，不能静默声称已持久化。

`export_checkpoint()` 返回 `_latest_checkpoint`，用于按需快照和停机兜底，但不能作为唯一保存
机制。恢复时先验证 `provider`、`member_agent_id` 和 `schema_version`：`REQUIRE_RESUME` 下缺失、
错配或不可迁移必须失败，不能悄悄创建新 session。

`data` 必须 JSON-safe，不能包含 token、完整 env、SDK client、event loop、文件句柄或任意 Python
对象。Provider 自己负责旧 schema 的兼容和迁移。

## 10. 核心实现骨架

以下省略 supervisor queue、follow-up 合并和具体 SDK 解析，但展示 P0 契约如何接线：

```python
from __future__ import annotations

import asyncio
import time
import uuid

from openjiuwen.agent_teams.external.protocol import (
    AbortMode,
    CheckpointReason,
    DeliveryMode,
    ExternalHarnessCard,
    ExternalHarnessContext,
    ExternalHarnessInput,
    ExternalHarnessProtocolError,
    ExternalHarnessStateError,
    HarnessCapability,
    HarnessCheckpoint,
    HarnessEvent,
    OutputEvent,
    OutputKind,
    SendReceipt,
    StateChangedEvent,
    TurnError,
    TurnEventKind,
    TurnLifecycleEvent,
    TurnResult,
    TurnStatus,
    UnsupportedHarnessCapabilityError,
)
from openjiuwen.agent_teams.harness import HarnessState


_END = object()


class AcmeHarness:
    card = ExternalHarnessCard(
        name="acme-code-agent",
        implementation_version="1.0.0",
        capabilities=frozenset(
            {
                HarnessCapability.STEER,
                HarnessCapability.GRACEFUL_ABORT,
                HarnessCapability.PERSISTENT_SESSION,
                HarnessCapability.CHECKPOINT,
                HarnessCapability.HOST_INTERACTIONS,
            }
        ),
    )

    def __init__(self, sdk_client):
        self._client = sdk_client
        self._context = None
        self._state = HarnessState.IDLE
        self._session_id = None
        self._latest_checkpoint = None
        self._events = asyncio.Queue()
        self._sequence = 0
        self._turn_task = None
        self._abort_requested = False
        self._lock = asyncio.Lock()
        self._consumer_lock = asyncio.Lock()

    @property
    def state(self):
        return self._state

    @property
    def session_id(self):
        return self._session_id

    async def start(self, context: ExternalHarnessContext):
        self._context = context
        restored = self._validate_checkpoint(context.checkpoint)
        self._session_id = await self._client.connect(
            checkpoint=restored,
            system_prompt=context.system_prompt,
            cwd=context.cwd,
        )
        await self._transition(HarnessState.IDLE)
        await self._publish_checkpoint(CheckpointReason.SESSION_ACTIVATED)

    async def send(self, content, *, mode=DeliveryMode.AUTO):
        message_id = str(uuid.uuid4())
        async with self._lock:
            if self._state is HarnessState.RUNNING:
                if mode is not DeliveryMode.STEER:
                    raise NotImplementedError("enqueue as follow-up in production")
                await self._client.steer(content.content)
                return SendReceipt(message_id, DeliveryMode.STEER)
            if self._state is not HarnessState.IDLE:
                raise ExternalHarnessStateError(f"cannot send while {self._state}")
            if mode is DeliveryMode.STEER:
                raise ExternalHarnessStateError("there is no active turn")
            await self._transition(HarnessState.RUNNING)
            self._turn_task = asyncio.create_task(self._run_turn(content, message_id))
        return SendReceipt(message_id, mode)

    async def _run_turn(self, content: ExternalHarnessInput, message_id: str):
        turn_id = str(uuid.uuid4())
        await self._emit(
            TurnLifecycleEvent(kind=TurnEventKind.STARTED),
            turn_id=turn_id,
            correlation_id=message_id,
        )
        try:
            final_output = None
            async for sdk_message in self._client.run(content.content):
                if self._is_server_request(sdk_message):
                    await self._handle_interaction(sdk_message, turn_id)
                elif text := self._text_delta(sdk_message):
                    final_output = text
                    await self._emit(
                        OutputEvent(kind=OutputKind.TEXT, content=text, is_delta=True),
                        turn_id=turn_id,
                        correlation_id=message_id,
                    )
            result = TurnResult(status=TurnStatus.COMPLETED, final_output=final_output)
            terminal = TurnEventKind.FINISHED
        except Exception as exc:
            if self._abort_requested:
                result = TurnResult(status=TurnStatus.INTERRUPTED)
                terminal = TurnEventKind.ABORTED
            else:
                result = TurnResult(
                    status=TurnStatus.FAILED,
                    error=TurnError(message=self._safe_error_message(exc)),
                )
                terminal = TurnEventKind.FAILED

        await self._emit(
            TurnLifecycleEvent(kind=terminal, result=result),
            turn_id=turn_id,
            correlation_id=message_id,
        )
        await self._publish_checkpoint(CheckpointReason.TURN_COMPLETED)
        await self._transition(HarnessState.IDLE)

    async def _handle_interaction(self, sdk_request, turn_id):
        if self._context.interactions is None:
            await self._client.decline(sdk_request.id, "host interaction unavailable")
            return
        request = self._normalize_interaction(sdk_request, turn_id)
        response = await self._context.interactions.handle(request)
        if response.request_id != request.request_id:
            raise RuntimeError("interaction response id mismatch")
        await self._client.respond(sdk_request.id, response)

    async def abort(self, *, mode=AbortMode.GRACEFUL):
        if mode is AbortMode.FORCE:
            raise UnsupportedHarnessCapabilityError("force abort is not supported")
        self._abort_requested = True
        await self._client.interrupt()

    async def events(self):
        if self._consumer_lock.locked():
            raise ExternalHarnessStateError("observation stream already has a consumer")
        async with self._consumer_lock:
            while (event := await self._events.get()) is not _END:
                yield event

    async def turn_events(self):
        turn_id = None
        async for event in self.events():
            payload = event.event
            if turn_id is None:
                if not (
                    isinstance(payload, TurnLifecycleEvent)
                    and payload.kind is TurnEventKind.STARTED
                ):
                    continue
                turn_id = event.turn_id

            yield event
            if (
                event.turn_id == turn_id
                and isinstance(payload, TurnLifecycleEvent)
                and payload.kind is not TurnEventKind.STARTED
            ):
                return

        if turn_id is not None:
            raise ExternalHarnessProtocolError(
                f"event stream closed before turn {turn_id} terminated"
            )

    async def stop(self):
        if self._state is HarnessState.TERMINATED:
            return
        if self._turn_task is not None:
            await self.abort()
            await self._turn_task
        await self._client.close()
        await self._transition(HarnessState.TERMINATED)
        await self._events.put(_END)

    async def pause(self):
        raise UnsupportedHarnessCapabilityError("pause/resume is not supported")

    async def resume(self, *, query=None):
        raise UnsupportedHarnessCapabilityError("pause/resume is not supported")

    async def export_checkpoint(self):
        return self._latest_checkpoint

    async def _emit(self, payload, **correlation):
        self._sequence += 1
        await self._events.put(
            HarnessEvent(
                sequence=self._sequence,
                timestamp=time.time(),
                session_id=self._session_id,
                event=payload,
                **correlation,
            )
        )

    async def _transition(self, new_state):
        old_state, self._state = self._state, new_state
        await self._emit(StateChangedEvent(old=old_state, new=new_state))

    async def _publish_checkpoint(self, reason):
        self._latest_checkpoint = HarnessCheckpoint(
            provider=self.card.name,
            schema_version="1",
            member_agent_id=self._context.member_agent_id,
            session_id=self._session_id,
        )
        if self._context.checkpoint_sink is not None:
            await self._context.checkpoint_sink.save(self._latest_checkpoint, reason=reason)

    # _validate_checkpoint/_normalize_interaction/_text_delta/
    # _is_server_request/_safe_error_message are provider-specific.
```

生产实现建议用 supervisor/control queue 统一处理 `send/abort/stop`，避免锁内调用 provider SDK
造成重入，并解决完成、abort 与 stop 同时发生时的双 terminal event 竞态。示例中的 follow-up 队列、
pending interaction cancel、bounded event backpressure 和 checkpoint retry 都需要在生产代码补齐。

## 11. Provider 实现

Provider 只负责配置验证和构造未启动 Harness：

```python
class AcmeProvider:
    card = AcmeHarness.card

    def create(self, config):
        validated = AcmeConfig.model_validate(dict(config))
        return AcmeHarness(
            AcmeSdkClient(endpoint=validated.endpoint, model=validated.model)
        )
```

构造阶段不得连接网络、启动 subprocess 或绑定 event loop；这些操作放到 `start()`。

## 12. Claude Code 和 Codex 的参考映射

| 协议语义 | Claude Agent SDK | Codex Python SDK |
|---|---|---|
| provider session | client connect + session id | thread start/resume + thread id |
| `send` | `query()` | thread turn |
| 持续 `events()` | `receive_messages()` 归一化 | thread/turn notification router |
| 单 Turn `turn_events()` | `receive_response()` 到 `ResultMessage`（含） | 到 turn terminal notification（含） |
| terminal `TurnResult` | `ResultMessage` 归一化 | turn completion/result 归一化 |
| tool approval | `can_use_tool` control request | command/file approval server request |
| hook policy | SDK hook callback | adapter/host hook interception |
| hook observation | hook event message | provider event/diagnostic |
| MCP/dynamic request | control/MCP message | server request/tool call |
| abort | `interrupt()` + cancel pending request | turn interrupt + cancel pending request |
| checkpoint | session id/state 主动 save | thread id/state 主动 save |

表中是 adapter 内部映射，不是公共协议的一部分。Provider 原始对象只允许先转成 JSON-safe
`provider_data`/`ProviderEvent`；不能把 SDK class 暴露给公共消费者。

## 13. 契约测试清单

三方项目至少覆盖：

1. 实例满足 `isinstance(harness, ExternalHarnessProtocol)`；
2. `start` 后为 IDLE，`stop` 后为 TERMINATED，重复 stop 不报错；
3. `events()` 在多个 Turn 之间不结束，stop 后正常 EOF；
4. `turn_events()` 包含 STARTED 和 terminal，terminal 后立即 EOF；
5. 两种流视图不能并发消费，交替调用不会重复或重排 event；
6. event sequence 跨所有 payload 严格递增；
7. 每轮恰好一个 STARTED 和一个状态匹配的 terminal `TurnResult`；
8. AUTO/FOLLOW_UP/STEER 在 IDLE/RUNNING 下符合定义；
9. abort、stop 和正常完成竞争时不产生两个 terminal event；
10. capability 与真实行为一致，未支持命令显式报错；
11. interaction request/response 的 id 一致，cancel 幂等；
12. 缺 interaction handler 时不会默认授权或永久等待；
13. hook deny 确实阻止执行，hook event consumer 不参与授权；
14. checkpoint envelope JSON round-trip、member/provider/version 校验和 REQUIRE_RESUME 失败路径；
15. session 激活和 turn 完成会主动调用 sink，save 失败不会被误报为成功；
16. event consumer 慢时背压有界，不会无限占用内存；
17. SDK 鉴权、限流、崩溃和超时落为 FAILED + `TurnError`，并且诊断不泄露凭据；
18. 未安装 Claude/Codex 等可选 SDK 时，protocol 包仍可导入。

## 14. 当前限制与后续接线

本次协议包没有：

- 修改 `ExternalCliAgentSpec`；
- 注册 Python entry point group；
- 提供 `ExternalHarnessProtocol -> MemberRuntime` adapter；
- 改造 `build_cli_runtime`；
- 迁移 Claude Code/Codex runtime。

这些接线完成前，协议实现不能仅靠声明自动成为 team member。请把 provider 与 harness 实现放在
独立、可测试的模块中，避免依赖当前 CLI spawn 内部结构，以便后续直接接入 registry 和 adapter。
