# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Candidate-first admission, shared observations and bounded hybrid handover."""

import asyncio
import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from openjiuwen.core.foundation.llm.schema.message import ToolMessage
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.harness.tools.browser_move.decision.action_space import build_menu, build_request
from openjiuwen.harness.tools.browser_move.decision.config import BrowserDecisionConfig
from openjiuwen.harness.tools.browser_move.decision.guard import PAGE_STATE_JS
from openjiuwen.harness.tools.browser_move.decision.jev_client import DecisionUnavailable, JevClient, validate_action
from openjiuwen.harness.tools.browser_move.playwright_runtime import execution_journal as journal
from openjiuwen.harness.tools.browser_move.playwright_runtime.browser_working_context import BrowserWorkingContextStore
from openjiuwen.harness.tools.browser_move.playwright_runtime.evidence import observed_label
from openjiuwen.harness.tools.browser_move.playwright_runtime.phase_contract import set_phase
from openjiuwen.harness.tools.browser_move.playwright_runtime.policy_page_action import (
    BrowserPageActionTool, fixed_text_script,
)
from openjiuwen.harness.tools.browser_move.playwright_runtime.runtime import BrowserRuntimeRail as Rail
from tests.unit_tests.harness.tools.browser_move.test_browser_jev_policy import (
    TOOLS, choose_operation, grouped_answer, messages_for, setup_policy,
)
from tests.unit_tests.harness.tools.browser_move.test_browser_jev_phase_contract import session_for
from tests.unit_tests.harness.tools.browser_move.test_browser_page_state import _make_bare_runtime
from tests.unit_tests.harness.tools.browser_move.test_browser_september17_contracts import dom_page  # noqa: F401

PHASE = "__browser_phase_budget_state__"
LOCAL_TOOLS = [*TOOLS, {"name": "browser_page_action"}]


def page_operations(runtime):
    page = runtime._ensure_page_state()
    page.decision_snapshot["page_guard"] = {"document": "doc-a", "history_length": 1}
    return page


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["unknown", "failed", "stalled", "ambiguous"])
async def test_local_reader_is_offered_before_whole_state_fallback(restriction):
    policy, llm, client, runtime, context, captured = setup_policy()
    page_operations(runtime)
    state = context.get_session_ref().get_state(PHASE)
    if restriction == "unknown":
        state["execution_journal"] = [{"call_id": "uncertain-save", "impact": "business",
                                       "execution_state": "dispatched_unknown"}]
    if restriction == "ambiguous":
        state["task"] = '搜索“mouse”和“keyboard”，分别读取价格'
    if restriction == "stalled":
        captured["semantic_progress"] = {"consecutive_no_progress": 8}
    choose_operation(client, "EXTRACT_TEXT")
    messages = await messages_for(policy, context, captured)
    if restriction == "failed":
        messages.append(ToolMessage(tool_call_id="previous", content='{"ok":false}'))
    result = await policy.invoke(messages, tools=LOCAL_TOOLS)
    assert json.loads(result.tool_calls[0].arguments)["op"] == "read_text"
    assert result.metadata["browser_policy"]["evaluated"]
    client.evaluate.assert_awaited_once()
    llm.invoke.assert_not_awaited()
    if restriction == "unknown":
        assert "CLICK" not in client.evaluate.call_args.args[0]["questions"]["action"]["criteria"]
        assert state["execution_journal"][0]["execution_state"] == "dispatched_unknown"


@pytest.mark.asyncio
async def test_two_failed_clicks_remove_only_that_action_and_llm_recovery_reopens_it():
    policy, llm, client, runtime, context, captured = setup_policy()
    page = page_operations(runtime)
    session = context.get_session_ref()
    choose_operation(client, "CLICK")
    for index in range(2):
        call = (await policy.invoke(await messages_for(policy, context, captured), tools=LOCAL_TOOLS)).tool_calls[0]
        policy.record_execution(SimpleNamespace(tool_call=call), session, {"success": False, "executed": False})
    choose_operation(client, "EXTRACT_TEXT")
    result = await policy.invoke(await messages_for(policy, context, captured), tools=LOCAL_TOOLS)
    assert result.metadata["browser_policy"]["excluded"]["failed_target"] == 1
    assert "CLICK" not in client.evaluate.call_args.args[0]["questions"]["action"]["criteria"]
    assert "HOVER" in client.evaluate.call_args.args[0]["questions"]["action"]["criteria"]
    # A successful LLM recovery plus a changed observed capability permits re-entry.
    policy.record_execution(SimpleNamespace(tool_call=SimpleNamespace(id="llm-repair"),
                                            tool_name="browser_snapshot", tool_args={}), session, {"success": True})
    target = page.get_target(page.export_decision_targets()[0]["target_id"])
    target.decision_state["node_guard"]["expanded"] = True
    page.decision_snapshot["capture_id"] = "after-repair"
    choose_operation(client, "CLICK")
    result = await policy.invoke(await messages_for(policy, context, captured), tools=LOCAL_TOOLS)
    assert result.metadata["browser_policy"]["operation"] == "click"
    assert not session.get_state(PHASE)["decision_policy"]["failed_actions"]
    assert client.evaluate.await_count == 4


@pytest.mark.asyncio
async def test_new_binding_changes_legal_menu_even_when_page_state_is_identical():
    policy, llm, client, runtime, context, captured = setup_policy(goal="查询汇率")
    page = page_operations(runtime)
    target = page.get_target(page.export_decision_targets()[0]["target_id"])
    target.role = "searchbox"
    target.decision_state.update(tag="input", search_like=True, current_value="resolved currency pair")
    choose_operation(client, "HANDOFF")
    for _ in range(2):
        await policy.invoke(await messages_for(policy, context, captured), tools=LOCAL_TOOLS)
    assert client.evaluate.await_count == 1
    policy.record_execution(SimpleNamespace(tool_call=SimpleNamespace(id="llm-fill"),
        tool_name="browser_type", tool_args={"target_id": target.target_id, "text": "resolved currency pair"}),
        context.get_session_ref(), {"success": True})
    choose_operation(client, "PRESS_ENTER")
    result = await policy.invoke(await messages_for(policy, context, captured), tools=LOCAL_TOOLS)
    assert json.loads(result.tool_calls[0].arguments)["steps"][0]["key"] == "Enter"
    assert client.evaluate.await_count == 2


@pytest.mark.asyncio
async def test_concise_phase_allows_decision_for_long_parent_goal_without_resetting_deadline():
    policy, llm, client, runtime, context, captured = setup_policy(goal="complex objective " * 800)
    state = context.get_session_ref().get_state(PHASE)
    deadline = state["deadline_at"]
    set_phase(state, {"objective": "点击销量排序"}, runtime._ensure_page_state().export_decision_targets())
    result = await policy.invoke(await messages_for(policy, context, captured), tools=TOOLS)
    assert result.tool_calls and state["deadline_at"] == deadline
    assert len(json.dumps(client.evaluate.call_args.args[0]["state"])) < 6000


@pytest.mark.parametrize("provider", ["typesafe", "openrouter"])
@pytest.mark.asyncio
async def test_two_heads_use_one_provider_request_and_validate_selected_target(monkeypatch, provider):
    config = BrowserDecisionConfig(provider=provider, mode="hybrid", api_key_env="LOCAL_TEST_JEV")
    monkeypatch.setenv("LOCAL_TEST_JEV", "synthetic-not-a-credential")
    menu = build_menu([], "Read this page", limit=30, page={"url": "https://example.test/",
                      "page_guard": {"document": "d"}}, page_operations={"read_text", "snapshot", "wait"})
    payload = build_request(config.model, {"current_intent": "Read this page"}, menu)
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        assert request.url.path.endswith("/decisions" if provider == "openrouter" else "/systemone")
        response = grouped_answer(payload, "EXTRACT_TEXT")
        response["model"] = config.model
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await JevClient(config, client=http).evaluate(payload, deadline_at=time.time() + 2)
    key, _ = validate_action(result["answers"], payload["questions"], 0.65)
    assert menu.steps[key]["op"] == "read_text"
    assert calls == [payload] and len(payload["questions"]) == 4
    other = next(iter(payload["questions"]["target_SNAPSHOT"]["criteria"]))
    result["answers"]["target_EXTRACT_TEXT"]["choice"] = other
    with pytest.raises(DecisionUnavailable):
        validate_action(result["answers"], payload["questions"], 0.65)


@pytest.mark.parametrize("input_label", [{"name": "销量"}, {"text": "销量"}, {"accessible_name": "销量"}])
def test_observed_labels_share_one_normalization(input_label):
    assert observed_label(input_label) == "销量"


def metadata(url="https://example.test/search?q=widget", capture="capture-post", selected="销量"):
    return {"ok": True, "url": url, "title": "Results", "tabs": [{"index": 0, "url": url, "current": True}],
            "page_position": {"pixels_below": 300}, "semantic_state": {"selected_filters": {"sort": selected}},
            "decision_probe": {"ok": True, "url": url, "elements": [{"selector_hint": "#sort",
                "selector_hint_validated": True, "match_count": 1, "text": selected, "kind": "sort_tab",
                "role": "button", "visible": True, "enabled": True, "actionable": True, "clickable": True,
                "selected": True, "decision_state": {"tag": "button", "node_guard": {"document": "doc", "node": 1}}}],
                "decision_snapshot": {"capture_id": capture, "url": url, "page_text": "Observed results",
                                      "page_guard": {"document": "doc"}}}}


@pytest.mark.asyncio
async def test_sort_receipt_and_post_action_cards_share_revision_but_later_change_invalidates():
    runtime = _make_bare_runtime()
    runtime.ensure_runtime_ready = AsyncMock()
    page = runtime._ensure_page_state()
    page.observe(url="https://example.test/search?q=widget")
    runtime._invalidate_changed_listing(metadata(selected="默认"))
    page.mark_interaction()
    admitted_revision = page.interaction_revision
    runtime._capture_browser_metadata = AsyncMock(return_value=(metadata(), None))
    observation = await runtime.capture_reconciliation_browser_state(action_group_id="sort", include_decision=True)
    assert observation["page_state"]["interaction_revision"] == admitted_revision
    page.register_cards({"url": page.url, "cards": [{"title": "Widget", "primary_link": "https://example.test/1"}]})
    runtime._invalidate_changed_listing(metadata())
    assert page.interaction_revision == admitted_revision and page.export()["cards_observed"]
    runtime._invalidate_changed_listing(metadata(selected="价格"))
    assert page.interaction_revision == admitted_revision + 1 and not page.export()["cards_observed"]


@pytest.mark.asyncio
async def test_combined_rpc_reuses_facts_once_and_never_after_another_action():
    runtime = _make_bare_runtime()
    runtime.ensure_runtime_ready = AsyncMock()
    runtime._call_playwright_tool = AsyncMock()
    runtime._call_playwright_run_code_unsafe = AsyncMock(return_value={
        "ok": True, "url": metadata()["url"], "_runtime_observation": metadata(),
    })
    result = await runtime._call_fixed_with_observation("async page => ({ok:true,url:page.url()})")
    assert result["ok"] and "_runtime_observation" not in result
    captured = await runtime.capture_browser_state(action_group_id="action", include_decision=True)
    assert captured["decision_observation"]["capture_id"] == "capture-post"
    assert captured["tabs"][0]["current"] and captured["page_position"]["pixels_below"] == 300
    runtime._call_playwright_run_code_unsafe.assert_awaited_once()
    runtime._call_playwright_tool.assert_not_awaited()
    assert not runtime._has_post_observation()
    await runtime._call_fixed_with_observation("async page => ({ok:true})")
    runtime._ensure_page_state().mark_interaction()
    assert not runtime._has_post_observation()


@pytest.mark.asyncio
async def test_fixed_text_read_joins_existing_evidence_without_cart_mutation():
    runtime = _make_bare_runtime()
    runtime._service = SimpleNamespace(allowed_tool_names=None)
    page = runtime._ensure_page_state()
    page.observe(url="https://shop.test/cart")
    runtime._call_fixed_with_observation = AsyncMock(return_value={
        "ok": True, "url": page.url, "title": "Cart", "text": "Cart has three products", "read_only": True,
    })
    state = Rail._build_phase_state("Read the cart product count")
    state["last_page"] = {"url": page.url}
    session = session_for(state)
    args = {"generation_id": page.generation_id, "op": "read_text"}
    result = await BrowserPageActionTool(runtime).invoke(args, session=session)
    assert result.success and result.data["state_changed"] is False
    assert page.read_observation["text"] == "Cart has three products"
    assert not journal.is_write("browser_page_action", args)
    assert Rail._page_observation_evidence(state, result.data, "browser_page_action", args)
    assert not state.get("cart_mutation_started")


def test_fixed_reader_treats_query_as_data_and_runs_in_real_dom(dom_page):
    query = 'needle "); window.injected = true; //'
    dom_page.set_content("<h1>Visible content</h1><p></p>")
    dom_page.locator("p").evaluate("(node, value) => node.textContent = value", query)
    value = dom_page.evaluate("async code => await eval('(' + code + ')')({evaluate:(fn,arg)=>fn(arg)})",
                              fixed_text_script(query))
    assert value["ok"] and query in value["text"] and len(value["matches"]) == 1
    assert dom_page.evaluate("typeof window.injected") == "undefined"


@pytest.mark.asyncio
async def test_llm_stream_total_timeout_closes_producer_without_replaying_partial_action(monkeypatch):
    policy, llm, client, runtime, context, captured = setup_policy("llm")
    closed = []

    async def producer(**kwargs):
        try:
            yield AssistantMessageChunk(content="Thinking")
            await asyncio.sleep(1)
        finally:
            closed.append(True)

    llm.stream = producer
    monkeypatch.setattr(policy, "_llm_wait_limit", lambda remaining: 0.02)
    with pytest.raises(TimeoutError):
        async for chunk in policy.stream(await messages_for(policy, context, captured), tools=TOOLS):
            assert not chunk.tool_calls
    assert closed == [True]
    client.evaluate.assert_not_awaited()
    runtime._call_playwright_run_code_unsafe.assert_not_awaited()


def test_replan_count_is_consecutive_and_progress_does_not_replenish_action_budget():
    state = Rail._build_phase_state("Read three pages")
    state.update(replan_count=3, replan_required=True, replan_trial_pending=True, deadline_at=time.time() + 600)
    deadline = state["deadline_at"]
    budgets = copy.deepcopy(state["phases"])
    session = session_for(state)
    assert BrowserWorkingContextStore.sync_semantic_progress(session, {
        "revision": 1, "observable_progress": True, "progress": "progress", "consecutive_no_progress": 0,
    })
    state = session.get_state(PHASE)
    assert state["replan_count"] == 0 and state["deadline_at"] == deadline and state["phases"] == budgets


@pytest.mark.asyncio
async def test_fill_submit_sort_read_then_llm_repair_reenters_same_runtime():
    policy, llm, client, runtime, context, captured = setup_policy(goal='搜索“widget”，按销量排序并读取列表')
    page = page_operations(runtime)
    target = page.get_target(page.export_decision_targets()[0]["target_id"])
    target.role, target.name = "searchbox", "搜索"
    target.decision_state.update(tag="input", input_type="search", search_like=True, current_value="")
    session = context.get_session_ref()
    seen = []
    for index, operation in enumerate(("TYPE_TEXT", "PRESS_ENTER", "CLICK", "EXTRACT_TEXT")):
        choose_operation(client, operation)
        result = await policy.invoke(await messages_for(policy, context, captured), tools=LOCAL_TOOLS)
        call = result.tool_calls[0]
        inputs = SimpleNamespace(tool_call=call, tool_name=call.name, tool_args=call.arguments)
        await policy.validate_tool_call(inputs, session)
        args = json.loads(call.arguments)
        step = args["steps"][0] if "steps" in args else args
        seen.append(step["op"])
        # Exercise the same shared journal that normal tool rails use.
        journal.prepare(session, inputs, runtime)
        with journal.execution_scope(session, inputs):
            journal.mark_dispatched()
        journal.record_result(session, inputs, {"success": True}, {"ok": True, "executed": True})
        policy.record_execution(inputs, session, {"success": True, "executed": True})
        if operation == "TYPE_TEXT":
            assert step["value"] == "widget"
            target.decision_state["current_value"] = "widget"
        elif operation == "PRESS_ENTER":
            page.observe(url="https://example.test/search?q=widget")
            target.role, target.name, target.text, target.kind = "button", "销量", "销量", "sort_tab"
            target.decision_state = {"tag": "button", "node_guard": {"document": "doc-b", "node": 2}}
        elif operation == "CLICK":
            target.selected = True
        else:
            page.read_observation = {"text": "Widget costs 10", "url": page.url}
        page.decision_snapshot.update(capture_id=f"loop-{index}", url=page.url)
        captured["url"] = page.url
    assert seen == ["fill", "press", "click", "read_text"]
    llm.invoke.assert_not_awaited()
    assert len(session.get_state(PHASE)["execution_journal"]) == 3  # Fixed reader is not a write.
    choose_operation(client, "HANDOFF")
    result = await policy.invoke(await messages_for(policy, context, captured), tools=LOCAL_TOOLS)
    assert result.content == "original LLM answer"
    session.get_state(PHASE)["task"] = "Read the missing details for the selected product"
    choose_operation(client, "EXTRACT_TEXT")
    result = await policy.invoke(await messages_for(policy, context, captured), tools=LOCAL_TOOLS)
    assert result.metadata["browser_policy"]["route"] == "jev"
    assert client.evaluate.await_count == 6 and llm.invoke.await_count == 1


@pytest.mark.asyncio
async def test_completed_first_result_milestone_survives_return_to_listing():
    policy, llm, client, runtime, context, captured = setup_policy(goal="打开第一条搜索结果并读取后续链接")
    page = page_operations(runtime)
    page.observe(url="https://search.test/?q=widget")
    page.decision_snapshot["url"] = page.url
    captured["url"] = page.url
    state = context.get_session_ref().get_state(PHASE)
    state["structured_evidence"] = [{"kind": "page_metadata", "destination_verified": True,
                                     "entity_url": "https://result.test/", "phase_version": 0}]
    for index in range(25):
        Rail._record_structured_evidence(state, {
            "ok": True, "operation": "read_text", "result": {"text": f"Additional observed page content {index}"},
            "page_state": {"url": page.url},
        }, tool_name="browser_page_action", tool_args={"op": "read_text"})
    assert len(state["structured_evidence"]) == 20
    assert any(item.get("destination_verified") for item in state["structured_evidence"])
    await messages_for(policy, context, captured)
    assert next(reversed(policy._observations.values())).page["first_result_pending"] is False
    set_phase(state, {"objective": "打开新查询的第一条搜索结果"}, page.export_decision_targets())
    await messages_for(policy, context, captured)
    assert next(reversed(policy._observations.values())).page["first_result_pending"] is True


@pytest.mark.parametrize("probe_available", [True, False])
def test_fixed_action_probe_in_one_rpc_keeps_receipt_when_probe_fails(dom_page, probe_available):
    dom_page.set_content('<input aria-label="Search" type="search"><h1>Example</h1>')
    runtime = _make_bare_runtime()
    code = runtime._fixed_observation_script("""async page => {
      await page.evaluate(() => {window.actions = (window.actions || 0) + 1;
        document.querySelector('input').value = 'widget';});
      return {ok:true, executed:true, url:page.url()};
    }""")
    value = dom_page.evaluate("""async args => {
      const page = {evaluate: (fn,arg) => fn(arg), url: () => location.href, title: async () => document.title};
      if (args.probe) page.context = () => ({pages: () => [page]});
      return await eval('(' + args.code + ')')(page);
    }""", {"probe": probe_available, "code": code})
    assert value["ok"] and value["executed"] and dom_page.evaluate("window.actions") == 1
    assert ("_runtime_observation" in value) is probe_available
    if probe_available:
        elements = value["_runtime_observation"]["decision_probe"]["elements"]
        assert any(item["decision_state"].get("current_value") == "widget" for item in elements)


@pytest.mark.asyncio
async def test_tab_selection_does_not_dispatch_after_observed_index_is_reused():
    runtime = _make_bare_runtime()
    runtime._service = SimpleNamespace(allowed_tool_names=("browser_tabs",))
    runtime._capture_browser_metadata = AsyncMock(return_value=({
        "tabs": [{"index": 1, "url": "https://different.test/"}],
    }, None))
    runtime._call_playwright_tool = AsyncMock()
    result = await BrowserPageActionTool(runtime).invoke({
        "generation_id": runtime.generation_id, "op": "select_tab", "index": 1, "url": "https://observed.test/",
    })
    assert not result.success and result.data["executed"] is False
    runtime._call_playwright_tool.assert_not_awaited()


def test_page_capability_and_long_option_list_cannot_hide_fixed_readers():
    controls = [{"target_id": f"select-{n}", "name": f"field-{n}", "role": "combobox",
                 "enabled": True, "actionable": True, "decision_state": {"tag": "select",
                 "node_guard": {"document": "d", "node": n},
                 "options": [{"value": str(i), "label": str(i)} for i in range(80)]}}
                for n in range(6)]
    page = {"url": "https://example.test/", "page_guard": {"document": "d"},
            "page_position": {"pixels_below": 300}}
    menu = build_menu(controls, "Open https://other.test/ and read fields", limit=30, page=page,
                      allow_page_actions=True, page_operations={"read_text", "wait"})
    assert any(step["op"] == "read_text" for step in menu.steps.values())
    assert any(step["op"] == "wait" for step in menu.steps.values())
    assert not any(step["op"] in {"navigate", "scroll"} for step in menu.steps.values())
    assert sum(step["op"] == "select_option" for step in menu.steps.values()) <= 30


@pytest.mark.parametrize("foreign", ["query", "source"])
def test_landing_proof_cannot_reuse_an_unrelated_result_list(foreign):
    source = "https://search.test/search?q=widget"
    destination = "https://item.test/1"
    state = {"query_id": "current", "last_page": {"url": source}, "structured_evidence": [{
        "query_id": "other" if foreign == "query" else "current",
        "source": "https://search.test/search?q=other" if foreign == "source" else source,
        "cards": [{"title": "Widget", "primary_link": destination, "region": "main_result", "is_ad": False}],
    }]}
    assert not Rail._destination_selection(state, destination, "browser_navigate", {
        "url": destination, "_runtime_source_url": source,
    }, landed_url=destination)


@pytest.mark.parametrize("selected", [True, False])
def test_popup_landing_receipt_requires_the_shared_native_tab_to_be_selected(selected):
    source, destination = "https://search.test/search?q=widget", "https://item.test/1"
    state = Rail._build_phase_state("Open the first search result and return its title")
    state["last_page"] = {"url": source, "title": "Old results"}
    state["structured_evidence"] = [{"source": source, "cards": [{
        "title": "Result link", "primary_link": destination, "region": "main_result", "is_ad": False,
    }]}]
    result = {"ok": True, "execution_mode": "compact_rpc",
              "page_state": {"url": destination, "title": "Actual landing title"},
              "page_binding": {"tab_switched": True, "mcp_selected": selected, "mcp_current_url": destination}}
    evidence = Rail._page_metadata_evidence(state, result, "browser_batch_interact", {
        "_runtime_source_url": source, "_runtime_selected_url": destination,
    })
    assert bool(evidence) is selected
    if selected:
        assert evidence["values"] == {"url": destination, "title": "Actual landing title"}
        assert evidence["destination_verified"]


@pytest.mark.parametrize("operation,cap", [("browser_evaluate", 15.0), ("browser_navigate", 30.0)])
def test_browser_call_uses_existing_resilience_and_keeps_shorter_timeout(operation, cap):
    tool = SimpleNamespace(properties={}, input_params=None)
    context = SimpleNamespace(agent=SimpleNamespace(ability_manager=SimpleNamespace(get=lambda name: tool)))
    Rail._validate_model_tool_args(context, operation, {})
    assert tool.properties["resilience"]["timeout_s"] == cap
    tool.properties["resilience"]["timeout_s"] = 3
    Rail._validate_model_tool_args(context, operation, {})
    assert tool.properties["resilience"]["timeout_s"] == 3


def test_fixed_reader_tolerates_layout_change_but_rejects_another_document(dom_page):
    policy, llm, client, runtime, context, captured = setup_policy()
    page = page_operations(runtime)
    page.decision_snapshot["page_guard"] = dom_page.evaluate(PAGE_STATE_JS)
    scripts = []

    async def compile_reader():
        choose_operation(client, "EXTRACT_TEXT")
        call = (await policy.invoke(await messages_for(policy, context, captured), tools=LOCAL_TOOLS)).tool_calls[0]
        inputs = SimpleNamespace(tool_call=call, tool_name=call.name, tool_args=call.arguments)

        async def inspect(script):
            scripts.append(script)
            return {"ok": True}

        runtime._call_playwright_run_code_unsafe.side_effect = inspect
        await policy.validate_tool_call(inputs, context.get_session_ref())

    # Playwright's synchronous fixture owns the main thread's event loop.
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(lambda: asyncio.run(compile_reader())).result(timeout=10)
    dom_page.evaluate("document.body.style.height = '4000px'; window.scrollTo(0, 500)")
    execute = "async a => await eval('(' + a.code + ')')({url:()=>a.url,evaluate:fn=>fn()})"
    assert dom_page.evaluate(execute, {"code": scripts[-1], "url": page.url})["ok"]
    dom_page.evaluate("delete window.__openjiuwenDecisionNodes")
    assert not dom_page.evaluate(execute, {"code": scripts[-1], "url": page.url})["ok"]
