from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.workflow.schemas import WorkflowNodeRun


class WorkflowNode(ABC):
    name: str

    @abstractmethod
    def run(self, context: dict[str, Any]) -> WorkflowNodeRun:
        """Run the node and return an auditable node result."""
