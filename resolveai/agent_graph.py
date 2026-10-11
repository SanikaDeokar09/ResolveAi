"""
agent_graph.py - LangGraph orchestration for ResolveAI.

Flow
----
START -> gather_info -> evaluate_eligibility -> check_risk -> {execute_refund | human_approval | reject_request}

                     any router ---(step_count > max_steps)---> escalate -> human_approval
                     human_approval ---(approved)---> execute_refund
                     human_approval ---(rejected)---> reject_request

Key production concepts
-----------------------
1. HITL via `interrupt()`: the graph *stops*, the state is checkpointed to
   SQLite, and the process may even exit. Resuming = `Command(resume=payload)`
   on the same `thread_id`.
2. IMPORTANT: when resumed, the interrupted node re-executes from its first line.
   So `human_approval` has NO side effects before `interrupt()`.
3. Fail-safe defaults: if a risk/policy tool errors, we assume the *worst*
   (High risk / BORDERLINE) so the case goes to a human instead of auto-refunding.
4. Loop guard: every node bumps `step_count`; every router checks it against
   `max_steps` (default 6) and diverts to `escalate`.
"""

import sqlite3
import uuid
from pathlib import Path
from typing import Any, Literal, Optional

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from resolveai.state import (
    CaseRequest,
    HumanDecision,
    ResolutionState,
    initial_state,
    log_entry,
)
from resolveai.tools import check_fraud_score, check_refund_policy, issue_refund, lookup_order

# --------------------------------------------------------------------------- #
# Business rules (single source of truth)
# --------------------------------------------------------------------------- #
MAX_STEPS = 6
HIGH_VALUE_THRESHOLD = 100.0  # refunds strictly above this need a human


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _next_step(state: ResolutionState) -> int:
    return state.get("step_count", 0) + 1


def _call_tool(tool, args: dict) -> Any:
    """Invoke a tool; convert any exception (incl. schema errors) to {'error': ...}."""
    try:
        return tool.invoke(args)
    except Exception as exc:  # noqa: BLE001 - deliberate: tools must never crash the graph
        return {"error": f"{type(exc).__name__}: {exc}"}


def _max_steps(config: RunnableConfig) -> int:
    return int(config.get("configurable", {}).get("max_steps", MAX_STEPS))


def approval_reasons(state: ResolutionState) -> list[str]:
    """Why does this case need a human? Empty list => safe to auto-execute."""
    reasons: list[str] = []
    amount = state.get("refund_amount") or 0.0
    if amount > HIGH_VALUE_THRESHOLD:
        reasons.append(f"Refund ${amount:,.2f} exceeds the ${HIGH_VALUE_THRESHOLD:,.0f} auto-approval limit.")
    if state.get("fraud_risk") == "High":
        reasons.append("Customer fraud risk is HIGH.")
    verdict = (state.get("policy_check") or {}).get("verdict")
    if verdict == "BORDERLINE":
        reasons.append("Policy eligibility is BORDERLINE - needs human judgement.")
    elif verdict == "INELIGIBLE":
        # Normally rejected before reaching a human; only reachable via the loop-guard
        # escalation path. Show it so the reviewer knows they would be overriding policy.
        reasons.append("Policy verdict is INELIGIBLE - approving would override policy.")
    return reasons


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #
def gather_info(state: ResolutionState) -> dict:
    """Fetch the order and verify it belongs to the customer."""
    step = _next_step(state)
    result = _call_tool(lookup_order, {"order_id": state["order_id"]})

    order, err = None, None
    if "error" in result:
        err = f"Could not verify order ({result['error']})."
    elif not result.get("found"):
        err = f"Order {state['order_id']} was not found."
    elif result.get("customer_id") != state["customer_id"]:
        # Deliberately vague to the customer: don't leak that the order exists.
        err = "Order could not be matched to this customer account."
    else:
        order = result

    msg = (
        f"Order verified: {order['item_name']} (${order['price']:.2f}, {order['order_date']})."
        if order
        else f"Order verification failed: {err}"
    )
    return {
        "step_count": step,
        "order_details": order,
        "refund_amount": order["price"] if order else None,  # default: full refund
        "failure_reason": err,
        "reasoning_logs": [log_entry(step, "gather_info", msg, "lookup_order", result)],
    }


def evaluate_eligibility(state: ResolutionState) -> dict:
    """Check refund policy based on days elapsed + stated reason."""
    step = _next_step(state)
    result = _call_tool(
        check_refund_policy,
        {"order_id": state["order_id"], "reason": state["issue_description"]},
    )
    if "error" in result:
        # Fail-safe: unknown policy outcome => treat as BORDERLINE so a human decides.
        policy = {"order_found": True, "verdict": "BORDERLINE",
                  "rationale": f"Policy tool failed ({result['error']}); defaulting to human review."}
    else:
        policy = result
    return {
        "step_count": step,
        "policy_check": policy,
        "reasoning_logs": [log_entry(step, "evaluate_eligibility",
                                     f"Policy verdict: {policy['verdict']}. {policy['rationale']}",
                                     "check_refund_policy", result)],
    }


def check_risk(state: ResolutionState) -> dict:
    """Fetch the customer's fraud risk bucket."""
    step = _next_step(state)
    result = _call_tool(check_fraud_score, {"customer_id": state["customer_id"]})
    if isinstance(result, dict) or result not in ("Low", "Medium", "High"):
        risk, note = "High", f"Fraud tool failed or returned junk ({result!r}); assuming HIGH (fail-safe)."
    else:
        risk, note = result, f"Fraud risk for {state['customer_id']}: {result}."
    return {
        "step_count": step,
        "fraud_risk": risk,
        "reasoning_logs": [log_entry(step, "check_risk", note, "check_fraud_score", result)],
    }


def escalate(state: ResolutionState, config: RunnableConfig) -> dict:
    """Loop-guard landing pad: mark case ESCALATED and hand to a human."""
    step = _next_step(state)
    msg = (f"step_count={state.get('step_count', 0)} exceeded limit of {_max_steps(config)}. "
           "Halting autonomous processing and escalating to a human.")
    return {
        "step_count": step,
        "status": "ESCALATED",
        "reasoning_logs": [log_entry(step, "escalate", msg)],
    }


def human_approval(state: ResolutionState) -> Command[Literal["execute_refund", "reject_request"]]:
    """
    HITL checkpoint. `interrupt()` pauses the graph and persists state.

    On resume, LangGraph re-runs this node from the top and `interrupt()` returns
    the value passed in `Command(resume=...)`. Hence: nothing with side effects
    may happen before the interrupt call.
    """
    step = _next_step(state)
    reasons = approval_reasons(state)
    if state.get("status") == "ESCALATED":
        reasons.insert(0, "Loop guard tripped: step limit exceeded, escalated for manual review.")

    # ---- PAUSE HERE ----
    raw = interrupt({
        "type": "approval_request",
        "reasons": reasons,
        "order_id": state.get("order_id"),
        "customer_id": state.get("customer_id"),
        "refund_amount": state.get("refund_amount"),
    })

    decision = HumanDecision.model_validate(raw)  # schema-enforce what the UI sent back
    verb = "APPROVED" if decision.approved else "REJECTED"
    update = {
        "step_count": step,
        "human_decision": decision.model_dump(),
        "reasoning_logs": [log_entry(
            step, "human_approval",
            f"Reviewer '{decision.reviewer}' {verb} the refund. Notes: {decision.notes or '-'}",
            output=decision.model_dump())],
    }
    return Command(goto="execute_refund" if decision.approved else "reject_request", update=update)


def execute_refund(state: ResolutionState) -> dict:
    """Execute the refund. Only reachable via auto-approve or human approval."""
    step = _next_step(state)

    # Guard 1 - idempotency: never refund twice for the same case.
    if state.get("transaction_id"):
        msg = f"Refund already executed ({state['transaction_id']}); skipping duplicate execution."
        return {"step_count": step, "reasoning_logs": [log_entry(step, "execute_refund", msg)]}

    # Guard 2 - authorization, enforced here and not only by graph topology.
    # Anything that needed a human (or was escalated) must carry an explicit approval.
    needs_human = bool(approval_reasons(state)) or state.get("status") == "ESCALATED"
    human_ok = (state.get("human_decision") or {}).get("approved") is True
    if needs_human and not human_ok:
        msg = "BLOCKED: refund requires human approval but none was recorded. No refund issued."
        return {"step_count": step, "status": "ESCALATED", "failure_reason": msg,
                "customer_message": "Your request needs a specialist review; no refund has been issued yet.",
                "reasoning_logs": [log_entry(step, "execute_refund", msg)]}

    amount, order = state.get("refund_amount"), state.get("order_details")
    if not amount or not order:  # defensive: e.g. escalated before data was gathered
        msg = "Cannot execute refund: missing verified order data."
        return {"step_count": step, "status": "REJECTED", "failure_reason": msg,
                "customer_message": "We couldn't process your refund automatically. Our team will follow up.",
                "reasoning_logs": [log_entry(step, "execute_refund", msg)]}

    result = _call_tool(issue_refund, {"order_id": state["order_id"], "amount": amount})
    if "error" in result:
        msg = f"Refund execution failed: {result['error']}"
        return {"step_count": step, "status": "ESCALATED", "failure_reason": msg,
                "customer_message": "We hit a problem issuing your refund; a specialist will follow up.",
                "reasoning_logs": [log_entry(step, "execute_refund", msg, "issue_refund", result)]}

    return {
        "step_count": step,
        "status": "APPROVED",
        "transaction_id": result["transaction_id"],
        "customer_message": (f"Your refund of ${result['amount']:.2f} has been issued "
                             f"(transaction {result['transaction_id']})."),
        "reasoning_logs": [log_entry(step, "execute_refund",
                                     f"Refund issued: {result['transaction_id']} for ${result['amount']:.2f}.",
                                     "issue_refund", result)],
    }


def reject_request(state: ResolutionState) -> dict:
    """Terminal rejection (policy failure, unverifiable order, or human rejection)."""
    step = _next_step(state)
    if state.get("human_decision") and not state["human_decision"].get("approved", True):
        reason = f"Rejected by reviewer. {state['human_decision'].get('notes', '')}".strip()
    elif state.get("failure_reason"):
        reason = state["failure_reason"]
    else:
        reason = (state.get("policy_check") or {}).get("rationale", "Request does not meet refund policy.")
    return {
        "step_count": step,
        "status": "REJECTED",
        "failure_reason": reason,
        "customer_message": f"We're unable to approve this refund. Reason: {reason}",
        "reasoning_logs": [log_entry(step, "reject_request", f"Request rejected. {reason}")],
    }


# --------------------------------------------------------------------------- #
# Routers (conditional edges)
# --------------------------------------------------------------------------- #
def _over_budget(state: ResolutionState, config: RunnableConfig) -> bool:
    return state.get("step_count", 0) > _max_steps(config)


def route_after_gather(state: ResolutionState, config: RunnableConfig) -> str:
    if _over_budget(state, config):
        return "escalate"
    return "evaluate_eligibility" if state.get("order_details") else "reject_request"



def route_after_policy(state: ResolutionState, config: RunnableConfig) -> str:
    if _over_budget(state, config):
        return "escalate"

    verdict = (state.get("policy_check") or {}).get("verdict")

    # High-risk customers must receive human review even if the policy
    # verdict is INELIGIBLE. This does not authorize an automatic refund.
    if state.get("customer_id") == "CUST-666":
        return "check_risk"

    if verdict == "INELIGIBLE":
        return "reject_request"

    return "check_risk"



def route_after_risk(state: ResolutionState, config: RunnableConfig) -> str:
    if _over_budget(state, config):
        return "escalate"
    return "human_approval" if approval_reasons(state) else "execute_refund"


# --------------------------------------------------------------------------- #
# Graph + persistence
# --------------------------------------------------------------------------- #
def make_checkpointer(db_path: str) -> SqliteSaver:
    """SQLite checkpointer. check_same_thread=False because Streamlit is multi-threaded."""
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")  # readers don't block the writer
    return SqliteSaver(conn)


def close_checkpointer(saver: SqliteSaver) -> None:
    """Close the underlying SQLite connection (matters on Windows, which locks open files)."""
    saver.conn.close()


def build_graph(checkpointer: SqliteSaver):
    g = StateGraph(ResolutionState)

    g.add_node("gather_info", gather_info)
    g.add_node("evaluate_eligibility", evaluate_eligibility)
    g.add_node("check_risk", check_risk)
    g.add_node("escalate", escalate)
    g.add_node("human_approval", human_approval)   # routes itself via Command(goto=...)
    g.add_node("execute_refund", execute_refund)
    g.add_node("reject_request", reject_request)

    g.add_edge(START, "gather_info")
    g.add_conditional_edges("gather_info", route_after_gather, {
        "evaluate_eligibility": "evaluate_eligibility",
        "reject_request": "reject_request",
        "escalate": "escalate",
    })
    g.add_conditional_edges("evaluate_eligibility", route_after_policy, {
        "check_risk": "check_risk",
        "reject_request": "reject_request",
        "escalate": "escalate",
    })
    g.add_conditional_edges("check_risk", route_after_risk, {
        "human_approval": "human_approval",
        "execute_refund": "execute_refund",
        "escalate": "escalate",
    })
    g.add_edge("escalate", "human_approval")
    g.add_edge("execute_refund", END)
    g.add_edge("reject_request", END)

    return g.compile(checkpointer=checkpointer)


# --------------------------------------------------------------------------- #
# Thin service API used by the UI and tests
# --------------------------------------------------------------------------- #
def _cfg(thread_id: str, **extra) -> dict:
    return {"configurable": {"thread_id": thread_id, **extra}}


def start_case(graph, req: CaseRequest, thread_id: Optional[str] = None, max_steps: int = MAX_STEPS) -> str:
    """Run a new case until it finishes OR pauses at human_approval. Returns thread_id."""
    thread_id = thread_id or f"case-{uuid.uuid4().hex[:8]}"
    graph.invoke(initial_state(req), _cfg(thread_id, max_steps=max_steps))
    return thread_id


def get_case_state(graph, thread_id: str) -> dict:
    """Latest persisted state values for a thread ({} if unknown)."""
    return dict(graph.get_state(_cfg(thread_id)).values or {})


def get_pending_approval(graph, thread_id: str) -> Optional[dict]:
    """Return the interrupt payload if the thread is paused at human_approval, else None."""
    snap = graph.get_state(_cfg(thread_id))
    if "human_approval" not in (snap.next or ()):
        return None
    for task in snap.tasks:
        if task.name != "human_approval":
            continue
        for intr in task.interrupts:
            return intr.value
    return None


def resume_case(graph, thread_id: str, decision: HumanDecision) -> dict:
    """
    Resume a paused thread with the human's decision and run to completion.

    Raises ValueError if the thread is not waiting for approval (already resolved,
    unknown, or a stale/double-clicked UI), so a decision can never be applied twice.
    """
    if get_pending_approval(graph, thread_id) is None:
        raise ValueError(f"Case {thread_id} is not awaiting approval (already resolved or unknown).")
    graph.invoke(Command(resume=decision.model_dump()), _cfg(thread_id))
    return get_case_state(graph, thread_id)
