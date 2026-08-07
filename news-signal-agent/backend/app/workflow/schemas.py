from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class NodeStatus(str, Enum):
    pending = "pending"
    running = "running"
    success = "success"
    failed = "failed"
    skipped = "skipped"


class WorkflowNodeRun(BaseModel):
    node_name: str
    status: NodeStatus = NodeStatus.pending
    input_ids: list[str] = Field(default_factory=list)
    output_ids: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    user_override_ids: list[str] = Field(default_factory=list)


class WorkflowRun(BaseModel):
    id: str
    status: NodeStatus = NodeStatus.pending
    created_at: datetime = Field(default_factory=datetime.utcnow)
    nodes: list[WorkflowNodeRun] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
