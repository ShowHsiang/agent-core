# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Data contracts for optional ReAct progress monitoring."""

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

Gain = Literal["NEW_EVIDENCE", "STATE_PROGRESS", "NO_GAIN", "UNKNOWN"]
Relation = Literal["CONTRADICTION", "COMPATIBLE", "STATE_UPDATE", "UNKNOWN"]
Recommendation = Literal["CONTINUE", "VERIFY", "REPLAN", "BACKTRACK"]
GAIN_VALUES = {"NEW_EVIDENCE": 0.5, "STATE_PROGRESS": 0.25, "NO_GAIN": 0.0}


class RuntimeMonitorConfig(BaseModel):
    """Bounded, advisory monitoring; attaching a monitor is the opt-in switch."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    mode: Literal["active", "observe"] = "active"
    window_size: int = Field(default=5, ge=1, le=32)
    warmup_steps: int = Field(default=2, ge=0)
    low_gain_threshold: float = Field(default=0.25, gt=0, le=1)
    low_gain_steps: int = Field(default=3, ge=1)
    conflict_threshold: int = Field(default=2, ge=1)
    unknown_steps: int = Field(default=3, ge=1)
    repeat_threshold: int = Field(default=3, ge=2)
    min_valid_ratio: float = Field(default=0.8, gt=0, le=1)
    cooldown_steps: int = Field(default=2, ge=0)
    observation_steps: int = Field(default=2, ge=1, le=16)
    max_episode_interventions: int = Field(default=2, ge=1)
    max_steps: int = Field(default=32, ge=5, le=128)
    max_observations: int = Field(default=64, ge=8, le=256)
    max_step_observations: int = Field(default=8, ge=1, le=16)
    max_pairs: int = Field(default=3, ge=0, le=8)
    excerpt_chars: int = Field(default=800, ge=64, le=4000)
    input_chars: int = Field(default=12000, ge=2000, le=64000)
    assessment_timeout: float = Field(default=1.5, gt=0, le=60)
    review_timeout: float = Field(default=1.5, gt=0, le=60)
    fallback_timeout: float = Field(default=5, gt=0, le=60)
    notification_timeout: float = Field(default=0.25, gt=0, le=5)
    rubric_version: str = "progress-v1"

    @model_validator(mode="after")
    def validate_capacity(self):
        if self.max_steps < max(self.window_size, self.observation_steps + 1):
            raise ValueError("max_steps must retain the window and advice observation period")
        if self.max_observations < self.window_size * self.max_step_observations:
            raise ValueError("max_observations must retain observations from the entire window")
        return self


class AcceptanceCriterion(BaseModel):
    description: str
    version: str | None = None


class RuntimeGoal(BaseModel):
    task_id: str
    text: str
    version: int = Field(default=1, ge=1)
    criteria: dict[str, AcceptanceCriterion] = Field(default_factory=dict)
    explicit: bool = False


class Observation(BaseModel):
    """A sourced tool report, not an assertion that its text is true."""

    obs_id: str = ""
    source: str = ""
    text: str
    fingerprint: str = ""
    complete: bool = True
    repeatable: bool = False
    observed_at: str | None = None
    subject: str | None = None
    property: str | None = None
    scope: str | None = None
    version: str | None = None
    value: Any = None
    # A host adapter may attest that this exact source settles this versioned property.
    resolution_ref: str | None = Field(default=None, pattern=r"\S")
    state_before: str | None = None
    state_after: str | None = None


class Verification(BaseModel):
    """Supplied by a trusted adapter, never by a semantic evaluator."""

    criterion_id: str
    proof_ref: str = Field(min_length=1)
    version: str | None = None
    passed: bool


class ToolEvidence(BaseModel):
    observations: list[Observation] = Field(default_factory=list)
    verifications: list[Verification] = Field(default_factory=list)
    waiting: bool = False
    usage: dict[str, Any] | None = None


class EvidenceAdapter(Protocol):
    def extract(self, tool_call: Any, result: Any, message: Any) -> ToolEvidence:
        """Return sourced observations and, optionally, domain verification."""
        ...


class StepAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gains: dict[str, Gain] = Field(default_factory=dict)
    relations: dict[str, Relation] = Field(default_factory=dict)
    off_track: bool | None = None
    followed_advice: Literal["YES", "NO", "UNKNOWN"] = "UNKNOWN"
    addressed_target: bool | None = None
    backend: str = "unknown"
    model: str = ""
    rubric_version: str = ""
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] | None = None
    error: str | None = None


class StepRecord(BaseModel):
    step_id: str
    seq: int
    status: Literal["partial", "closed", "terminal", "aborted"] = "partial"
    actions: dict[str, dict[str, str]] = Field(default_factory=dict)
    completed_calls: list[str] = Field(default_factory=list)
    waiting_calls: list[str] = Field(default_factory=list)
    partial_results: dict[str, str] = Field(default_factory=dict)
    observations: list[Observation] = Field(default_factory=list)
    verifications: list[Verification] = Field(default_factory=list)
    verified_progress: list[str] = Field(default_factory=list)
    refuted: list[str] = Field(default_factory=list)
    eligible: bool = False
    input_complete: bool = True
    assessment_started: bool = False
    assessment: StepAssessment | None = None
    gain: float | None = None
    gain_is_lower_bound: bool = False
    repeat_key: str = ""
    advice_id: str | None = None
    request_id: str | None = None
    usage: dict[str, Any] | None = None
    review: dict[str, Any] | None = None
    tool_usage: dict[str, dict[str, Any]] = Field(default_factory=dict)


class WindowStats(BaseModel):
    step_ids: list[str] = Field(default_factory=list)
    gain_sum: float = 0
    gain_mean: float | None = None
    valid_ratio: float = 0
    verified_progress_count: int = 0
    low_gain_streak: int = 0
    unknown_streak: int = 0
    repeat_count: int = 0
    new_conflict_count: int = 0
    contains_lower_bound: bool = False


class AdviceOption(BaseModel):
    option_id: str
    recommendation: Recommendation
    target_ref: str
    instruction: str
    evidence_ids: list[str] = Field(default_factory=list)


class Advice(BaseModel):
    advice_id: str
    option: AdviceOption
    goal_version: int
    created_seq: int
    memory_revision: int
    trigger_codes: list[str]
    state: Literal[
        "created", "delivered", "observing", "improved", "no_observed_improvement",
        "inconclusive", "expired", "superseded", "observed_only",
    ] = "created"
    observed_steps: list[str] = Field(default_factory=list)
    delivered_requests: list[str] = Field(default_factory=list)
    missed_requests: int = 0
    followed: Literal["YES", "NO", "UNKNOWN"] = "UNKNOWN"
    degraded: bool = False


class Conflict(BaseModel):
    key: str
    first_step: str
    evidence_ids: list[str]
    basis: str
    resolved: bool = False
    issue_key: str | None = None
    resolution_ref: str | None = None
    reopened_step: str | None = None


class StepEvaluationInput(BaseModel):
    goal: RuntimeGoal
    step: StepRecord
    history: list[Observation]
    pairs: dict[str, tuple[Observation, Observation]]
    advice: Advice | None = None
    revision: int
    rubric_version: str


class ReviewInput(BaseModel):
    goal: RuntimeGoal
    stats: WindowStats
    signals: list[str]
    observations: list[Observation]
    options: list[AdviceOption]
    revision: int
    rubric_version: str


class InterventionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    needed: bool | None = None
    option_id: str | None = None
    backend: str = "unknown"
    model: str = ""
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] | None = None
    error: str | None = None


class RuntimeEvaluator(Protocol):
    async def assess_step(self, data: StepEvaluationInput) -> StepAssessment: ...

    async def review_intervention(self, data: ReviewInput) -> InterventionReview: ...
