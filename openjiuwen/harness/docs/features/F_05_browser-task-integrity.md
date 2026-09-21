# F_05 Browser Task Integrity

Current consolidated reference (reviewed 2026-09-21):
[Browser subagent](../../../../docs/zh/2.开发指南/API文档/openjiuwen.harness/subagents/浏览器子智能体.md).
It covers the local implementation, including uncommitted changes. This feature
record retains decisions and verification boundaries rather than duplicating
the complete tool and lifecycle reference.

## Metadata

| Item | Value |
| --- | --- |
| Date | 2026-09-16 |
| Scope | Browser evidence, targets, cancellation and shared-browser task ownership |
| Specs | S_05 tools contract, S_18 subagents and lifecycle |
| Refs | openJiuwen-ai/jiuwenswarm#6006; local September 16 browser traces |

## Decisions

- Reuse the existing BrowserService registry for exclusive task ownership of a
  shared MCP browser. Serialize the entire task, not individual navigation calls.
  Distinct browser keys remain independent. Preserve Chrome and cookies.
- Stop and join streaming producers before releasing task resources. Closing a
  consumer must not leave a model/tool loop running without an output sink.
- Keep evidence in existing task slots. Bind records to their page/entity and
  replace contradicted values without mixing product identities or variants.
- Preserve executable URLs and selectors. Validate batch schemas up front, but
  validate dependent DOM targets at execution time after earlier waits/actions.
- Keep model strategy selection and natural-language explanation unconstrained
  by new planning protocols. No new manager, memory layer or completion gate.

## September 18 Contract Repairs

- Repair generated Card JavaScript on selected-control branches; preserve script
  failures rather than converting them to empty results or retrying the same code.
- Accept current native AX refs and PageState targets through the same resolver.
  Normalize unambiguous input aliases before schema validation; sort waits use
  the target's live selected state, not a model-invented selector or URL.
- Navigate genuine destination links only. Fragment/JavaScript links and explicit
  state controls remain clicks. A later failed wait must not replay a prior click.
- Pass the original user goal through existing TaskTool run context. Infer
  comparison slots only for requested comparisons, not enumerated page controls.
  Keep navigation completion distinct from result-card and destination evidence.
- Reuse existing evidence slots and provenance. Corrected same-entity observations
  supersede wrong values; unrelated pages and entities cannot certify completion.
- Unexecuted textual tool intents get at most one same-run protocol correction.
  Focused resume retains the user's constraints and the caller's repair instruction
  under the existing shared deadline, without creating a second task-state store.

Verification uses the September 17 log signatures and generated-script branch
tests. Live site latency and answer accuracy still require paired user queries;
passing deterministic tests is not a claim of live-site completion rate.

## September 18 Observation and Completion Cooperation

- Native AX is decoded before registration and remains available as a bounded,
  recallable excerpt. Probe augments it instead of replacing native observations.
- Card quality chooses containers, never result rank. Rank is scoped to a known
  result region and follows rendered order; ambiguous regions have no global rank.
  Query-filtered and scrolled viewport subsets do not claim a global first result.
- Field extraction requires local provenance. Same-entity detail observations
  can correct weaker listing values without accepting unrelated entities.
- Inferred field coverage is advice to finish, not a reason to remove tools.
  Runtime still owns the final transport payload and hard cancellation/deadlines.
- Existing query state permits one evidence-directed correction, not unlimited
  restarts. Timeout returns captured progress; user cancellation stops execution.
- Keep one current PageState projection, invalidate stale listing targets on a
  confirmed filter change, and preserve raw observations before lossy projection
  in bounded task-scoped recall storage. Permanent raw audit remains opt-in.
  Temporary observation artifacts expire after 24 hours and are pruned lazily on
  writes (a workspace sweep at most hourly), including previous task folders; each task is capped at 128
  artifacts / 64 MiB. A failed write preserves the native result, not a lost preview.
- Native primitives handle single actions; Batch handles deliberate sequences.
  Answer panels are valid sources for answer tasks, not natural-result ranks.

## Rejected Alternatives (Observation Repairs)

No extra planner, LLM judge, completion manager, mandatory second verification,
or reduced context budget is introduced. Lower-confidence extraction must not
override the model's ability to inspect a destination or correct a bad field.

Per-tool browser locks cannot prevent interleaved task navigation. Per-task
Chrome/profile recreation breaks login reuse. More forced verification, smaller
context windows, and larger retry budgets do not repair these contracts.

## Verification and Boundaries

Deterministic tests cover task exclusion/cancellation, streaming closure, source
isolation, executable data and sequential batch validation. Live site and model
latency validation remains a separate user-run comparison. Cookies remain shared
intentionally for the same browser key; this is execution isolation, not private
per-session storage. MCP error preservation from PR #1215 and colleagues' advisory
inspection/AX progress changes are separate work and are not reimplemented here.

The shared-key exclusion is process-local. It does not provide private login
storage or coordinate separately launched host processes. Hosts pointing several
keys at the same external CDP endpoint must supply a shared browser identity.
Cancellation stops and joins the local producer; it cannot undo a browser action
that has already reached the remote page. Ambiguous actions are not replayed.
