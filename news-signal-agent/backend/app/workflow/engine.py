from __future__ import annotations

from app.workflow.schemas import WorkflowNodeRun


DEFAULT_NODE_ORDER = [
    "collect",
    "normalize",
    "dedup",
    "update_event_store",
    "generate_event_card",
    "entity_link",
    "rule_classification",
    "llm_classification",
    "classification_merge",
    "feedback_override",
    "score",
    "market_validate",
    "backtest_validate",
    "rank",
    "memory_store",
    "notify",
]


class WorkflowEngine:
    """Small workflow facade.

    The first implementation only exposes the DAG shape. Later iterations will
    persist each node's inputs, outputs, decisions, and user overrides.
    """

    def __init__(self, node_order: list[str]) -> None:
        self.node_order = node_order

    @classmethod
    def default(cls) -> "WorkflowEngine":
        return cls(DEFAULT_NODE_ORDER)

    def preview(self) -> dict:
        nodes = [WorkflowNodeRun(node_name=name).model_dump(mode="json") for name in self.node_order]
        edges = [
            {"source": self.node_order[i], "target": self.node_order[i + 1]}
            for i in range(len(self.node_order) - 1)
        ]
        return {"nodes": nodes, "edges": edges}
