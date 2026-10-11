"""
End-to-end flow tests. Run from the project root:  pytest -q

Each test uses a throwaway SQLite file so runs are isolated.
"""

import pytest

from resolveai import agent_graph
from resolveai.agent_graph import (
    build_graph,
    close_checkpointer,
    get_case_state,
    get_pending_approval,
    make_checkpointer,
    resume_case,
    start_case,
)
from resolveai.state import CaseRequest, HumanDecision
from resolveai.tools import issue_refund as real_issue_refund


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def graph(db):
    saver = make_checkpointer(db)
    yield build_graph(saver)
    close_checkpointer(saver)  # Windows keeps the file locked until the connection closes


class RefundSpy:
    """Wraps the real issue_refund tool and records every invocation."""

    def __init__(self):
        self.calls = []

    def invoke(self, args):
        self.calls.append(dict(args))
        return real_issue_refund.invoke(args)


@pytest.fixture
def spy(monkeypatch):
    s = RefundSpy()
    monkeypatch.setattr(agent_graph, "issue_refund", s)
    return s


def submit(graph, customer, order, issue="Item stopped working", **kw):
    req = CaseRequest(customer_id=customer, order_id=order, issue_description=issue)
    return start_case(graph, req, **kw)


def test_auto_approve_small_low_risk(graph):
    tid = submit(graph, "CUST-001", "ORD-1001")
    s = get_case_state(graph, tid)
    assert get_pending_approval(graph, tid) is None
    assert s["status"] == "APPROVED"
    assert s["transaction_id"].startswith("TXN-")


def test_policy_failure_rejects(graph):
    tid = submit(graph, "CUST-001", "ORD-1006", "Changed my mind")
    s = get_case_state(graph, tid)
    assert s["status"] == "REJECTED"
    assert s["transaction_id"] is None


def test_unknown_or_foreign_order_rejects(graph):
    assert get_case_state(graph, submit(graph, "CUST-001", "ORD-9999"))["status"] == "REJECTED"
    # ORD-1002 belongs to CUST-002, not CUST-001
    assert get_case_state(graph, submit(graph, "CUST-001", "ORD-1002"))["status"] == "REJECTED"


@pytest.mark.parametrize(
    "customer,order,issue,expected_reason",
    [
        ("CUST-002", "ORD-1002", "Monitor flickers", "exceeds"),      # > $100
        ("CUST-666", "ORD-1004", "Changed my mind", "HIGH"),          # high fraud
        ("CUST-002", "ORD-1005", "Changed my mind", "BORDERLINE"),    # borderline policy
        ("CUST-001", "ORD-1003", "Arrived damaged", "BORDERLINE"),    # defect window makes it borderline
    ],
)
def test_pauses_for_human(graph, customer, order, issue, expected_reason):
    tid = submit(graph, customer, order, issue)
    payload = get_pending_approval(graph, tid)
    assert payload is not None, "graph should be paused at human_approval"
    assert any(expected_reason in r for r in payload["reasons"])
    assert get_case_state(graph, tid)["transaction_id"] is None  # nothing executed yet


def test_human_approve_executes_refund(graph):
    tid = submit(graph, "CUST-002", "ORD-1002", "Monitor flickers")
    s = resume_case(graph, tid, HumanDecision(approved=True, reviewer="alice"))
    assert s["status"] == "APPROVED"
    assert s["transaction_id"]
    assert get_pending_approval(graph, tid) is None
    assert s["human_decision"]["reviewer"] == "alice"


def test_human_reject_does_not_refund(graph):
    tid = submit(graph, "CUST-666", "ORD-1004", "Changed my mind")
    s = resume_case(graph, tid, HumanDecision(approved=False, notes="suspicious"))
    assert s["status"] == "REJECTED"
    assert s["transaction_id"] is None


def test_pause_survives_process_restart(db):
    """Pause with one graph/connection, resume with a brand-new one on the same file."""
    saver1 = make_checkpointer(db)
    g1 = build_graph(saver1)
    tid = submit(g1, "CUST-002", "ORD-1002", "Monitor flickers")
    assert get_pending_approval(g1, tid) is not None
    close_checkpointer(saver1)  # simulate the process going away
    del g1

    saver2 = make_checkpointer(db)
    try:
        g2 = build_graph(saver2)
        assert get_pending_approval(g2, tid) is not None
        s = resume_case(g2, tid, HumanDecision(approved=True))
        assert s["status"] == "APPROVED"
    finally:
        close_checkpointer(saver2)


def test_loop_guard_escalates(graph):
    # Tiny budget forces the guard to trip even on an otherwise auto-approvable case.
    tid = submit(graph, "CUST-001", "ORD-1001", max_steps=2)
    payload = get_pending_approval(graph, tid)
    assert payload is not None
    assert any("Loop guard" in r for r in payload["reasons"])
    assert get_case_state(graph, tid)["status"] == "ESCALATED"
    assert get_case_state(graph, tid)["transaction_id"] is None


def test_reasoning_log_is_append_only_and_ordered(graph):
    tid = submit(graph, "CUST-002", "ORD-1002", "Monitor flickers")
    resume_case(graph, tid, HumanDecision(approved=True))
    logs = get_case_state(graph, tid)["reasoning_logs"]
    steps = [e["step"] for e in logs]
    assert steps == sorted(steps) and len(set(steps)) == len(steps)
    assert [e["node"] for e in logs][-2:] == ["human_approval", "execute_refund"]


# --------------------------------------------------------------------------- #
# Refund-safety guarantees (spy on the real issue_refund tool)
# --------------------------------------------------------------------------- #
def test_auto_approve_issues_exactly_one_refund(graph, spy):
    submit(graph, "CUST-001", "ORD-1001")
    assert len(spy.calls) == 1
    assert spy.calls[0] == {"order_id": "ORD-1001", "amount": 59.99}


@pytest.mark.parametrize(
    "customer,order,issue",
    [
        ("CUST-002", "ORD-1002", "Monitor flickers"),
        ("CUST-666", "ORD-1004", "Changed my mind"),
        ("CUST-002", "ORD-1005", "Changed my mind"),
    ],
)
def test_no_refund_while_paused(graph, spy, customer, order, issue):
    tid = submit(graph, customer, order, issue)
    assert get_pending_approval(graph, tid) is not None
    assert spy.calls == []


def test_refund_only_after_approval_and_only_once(graph, spy):
    tid = submit(graph, "CUST-002", "ORD-1002", "Monitor flickers")
    assert spy.calls == []
    resume_case(graph, tid, HumanDecision(approved=True))
    assert len(spy.calls) == 1
    # A second resume (double click / stale UI) must be refused and must not refund again.
    with pytest.raises(ValueError):
        resume_case(graph, tid, HumanDecision(approved=True))
    assert len(spy.calls) == 1


def test_resume_of_unknown_or_finished_case_is_refused(graph, spy):
    with pytest.raises(ValueError):
        resume_case(graph, "no-such-case", HumanDecision(approved=True))
    tid = submit(graph, "CUST-001", "ORD-1001")  # auto-approved, never paused
    with pytest.raises(ValueError):
        resume_case(graph, tid, HumanDecision(approved=True))
    assert len(spy.calls) == 1


def test_rejections_never_refund(graph, spy):
    submit(graph, "CUST-001", "ORD-1006", "Changed my mind")   # policy failure
    submit(graph, "CUST-001", "ORD-9999")                      # unknown order
    submit(graph, "CUST-001", "ORD-1002")                      # not this customer's order
    tid = submit(graph, "CUST-666", "ORD-1004", "x" * 10)      # human rejects
    resume_case(graph, tid, HumanDecision(approved=False, notes="no"))
    assert spy.calls == []


def test_execute_refund_blocks_without_authorization(spy):
    """Defence in depth: even if routing were wrong, execute_refund refuses unapproved high-risk cases."""
    state = {
        "order_id": "ORD-1004", "customer_id": "CUST-666", "refund_amount": 79.0,
        "order_details": {"item_name": "Smart Watch", "price": 79.0},
        "fraud_risk": "High", "policy_check": {"verdict": "ELIGIBLE"},
        "status": "PENDING", "step_count": 3, "human_decision": None,
    }
    out = agent_graph.execute_refund(state)
    assert spy.calls == []
    assert out["status"] == "ESCALATED"
    assert "BLOCKED" in out["failure_reason"]


def test_execute_refund_is_idempotent_when_txn_exists(spy):
    state = {
        "order_id": "ORD-1001", "customer_id": "CUST-001", "refund_amount": 59.99,
        "order_details": {"item_name": "Wireless Headphones", "price": 59.99},
        "fraud_risk": "Low", "policy_check": {"verdict": "ELIGIBLE"},
        "status": "APPROVED", "step_count": 4, "transaction_id": "TXN-ALREADY",
    }
    agent_graph.execute_refund(state)
    assert spy.calls == []


def test_tool_failure_is_fail_safe(graph, spy, monkeypatch):
    """If the fraud tool blows up, the agent must NOT auto-refund; it goes to a human."""

    class Boom:
        def invoke(self, args):
            raise RuntimeError("fraud service down")

    monkeypatch.setattr(agent_graph, "check_fraud_score", Boom())
    tid = submit(graph, "CUST-001", "ORD-1001")
    payload = get_pending_approval(graph, tid)
    assert payload is not None and any("HIGH" in r for r in payload["reasons"])
    assert get_case_state(graph, tid)["fraud_risk"] == "High"
    assert spy.calls == []


def test_loop_guard_escalation_then_approve_executes_once(graph, spy):
    tid = submit(graph, "CUST-001", "ORD-1001", max_steps=2)
    assert spy.calls == []
    s = resume_case(graph, tid, HumanDecision(approved=True, reviewer="bob"))
    assert s["status"] == "APPROVED" and len(spy.calls) == 1


def test_execution_order_and_logging(graph):
    """Auto path: nodes run in the documented order, with the right tool per step."""
    tid = submit(graph, "CUST-001", "ORD-1001")
    logs = get_case_state(graph, tid)["reasoning_logs"]
    assert [e["node"] for e in logs] == ["gather_info", "evaluate_eligibility", "check_risk", "execute_refund"]
    assert [e["tool"] for e in logs] == ["lookup_order", "check_refund_policy", "check_fraud_score", "issue_refund"]
    assert [e["step"] for e in logs] == [1, 2, 3, 4]
    assert get_case_state(graph, tid)["step_count"] == 4


def test_policy_rejection_skips_risk_check(graph):
    tid = submit(graph, "CUST-001", "ORD-1006", "Changed my mind")
    nodes = [e["node"] for e in get_case_state(graph, tid)["reasoning_logs"]]
    assert nodes == ["gather_info", "evaluate_eligibility", "reject_request"]


def test_human_reject_path_order(graph):
    tid = submit(graph, "CUST-666", "ORD-1004", "Changed my mind")
    resume_case(graph, tid, HumanDecision(approved=False, notes="suspicious"))
    nodes = [e["node"] for e in get_case_state(graph, tid)["reasoning_logs"]]
    assert nodes == ["gather_info", "evaluate_eligibility", "check_risk", "human_approval", "reject_request"]
