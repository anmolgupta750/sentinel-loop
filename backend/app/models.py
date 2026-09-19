from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class StepStatus(str, Enum):
    queued = "queued"
    running = "running"
    passed = "passed"
    retrying = "retrying"
    escalated = "escalated_to_human"
    awaiting_approval = "awaiting_approval"
    approved = "approved"


class TaskRequest(BaseModel):
    task_description: str
    document_ids: list[str] = Field(default_factory=list)


class ApprovalRequest(BaseModel):
    approved: bool
    note: str = ""


class RetryRequest(BaseModel):
    note: str = ""


class AuditEvent(BaseModel):
    id: str
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    task_id: str = ""
    step_id: int | None = None
    event_type: str = "info"
    actor: str
    action: str
    detail: str
    level: str = "info"
    metadata: dict[str, Any] = Field(default_factory=dict)


class StepCriteria(BaseModel):
    instruction_match: bool = True
    factual_quality: bool = True
    source_grounding: bool = True
    format_compliance: bool = True


class Step(BaseModel):
    id: int
    instruction: str
    type: str = "text"
    status: StepStatus = StepStatus.queued
    attempts: int = 0
    output: Any = None
    verdict: str | None = None
    feedback: str | None = None
    criteria: dict[str, bool] | None = None
    grounding_required: bool = False
    fabrication_allowed: bool = False
    unsupported_claims: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    rag_sources: list[str] = Field(default_factory=list)
    rag_chunks: list[dict[str, Any]] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


class Workflow(BaseModel):
    task_id: str
    task_description: str
    document_ids: list[str] = Field(default_factory=list)
    grounding_required: bool = False
    fabrication_allowed: bool = False
    status: str = "running"
    current_step: int = 1
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    steps: list[Step] = []
    final_result: str | None = None
    synthesis_grounded: bool | None = None
    revision_note: str | None = None
    events: list[AuditEvent] = []
    error_message: str | None = None
