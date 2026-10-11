
# from typing import Any, TypedDict
from pydantic import BaseModel, Field
from typing import Any, TypedDict, Annotated
from operator import add


class CaseRequest(BaseModel):
    customer_id: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    issue_description: str = Field(min_length=1)


class HumanDecision(BaseModel):
    approved: bool
    reviewer: str = "reviewer"
    notes: str = ""


class ResolutionState(TypedDict, total=False):
    customer_id: str
    order_id: str
    issue_description: str
    order_details: dict[str, Any] | None
    refund_amount: float | None
    failure_reason: str | None
    policy_check: dict[str, Any] | None
    fraud_risk: str | None
    status: str
    transaction_id: str | None
    customer_message: str | None
    human_decision: dict[str, Any] | None
    reasoning_logs: Annotated[list[dict[str, Any]], add]
    step_count: int


def initial_state(request: CaseRequest) -> ResolutionState:
    return {
        "customer_id": request.customer_id,
        "order_id": request.order_id,
        "issue_description": request.issue_description,
        "order_details": None,
        "refund_amount": None,
        "failure_reason": None,
        "policy_check": None,
        "fraud_risk": None,
        "status": "PENDING",
        "transaction_id": None,
        "customer_message": None,
        "human_decision": None,
        "reasoning_logs": [],
        "step_count": 0,
    }


def log_entry(
    step: int,
    node: str,
    message: str,
    tool: str | None = None,
    output: Any = None,
) -> dict[str, Any]:
    entry = {
        "step": step,
        "node": node,
        "message": message,
        "tool": tool,
    }

    if output is not None:
        entry["output"] = output

    return entry
