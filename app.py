"""
app.py - ResolveAI operator dashboard (Streamlit).

Run:  streamlit run app.py

Layout
------
Left  : customer resolution trigger/chat
Right : pending human approvals (reasoning log, tool outputs, order info, risk) + Approve/Reject
Bottom: recent case history
"""

import os
from pathlib import Path

import streamlit as st
from pydantic import ValidationError

from resolveai.agent_graph import (
    MAX_STEPS,
    build_graph,
    get_case_state,
    get_pending_approval,
    make_checkpointer,
    resume_case,
    start_case,
)
from resolveai.registry import CaseRegistry
from resolveai.state import CaseRequest, HumanDecision

# Anchor the default DB to the project folder (not the CWD) so VS Code / PowerShell
# launches from any directory use the same file. Override with RESOLVEAI_DB.
DB_PATH = os.environ.get("RESOLVEAI_DB") or str(Path(__file__).resolve().parent / "data" / "resolveai.db")

st.set_page_config(page_title="ResolveAI Operator Console", page_icon="🛟", layout="wide")


# --------------------------------------------------------------------------- #
# Singletons: Streamlit reruns this script on every interaction, so the graph and
# its SQLite connection must be created once and cached.
# --------------------------------------------------------------------------- #
@st.cache_resource
def get_runtime(db_path: str):
    # Keyed by db_path, so a different DB gets its own graph/connection.
    return build_graph(make_checkpointer(db_path)), CaseRegistry(db_path)


graph, registry = get_runtime(DB_PATH)

if "chat" not in st.session_state:
    st.session_state.chat = []  # [(role, text)]

RISK_BADGE = {"Low": "🟢 Low", "Medium": "🟡 Medium", "High": "🔴 High"}
STATUS_BADGE = {"PENDING": "⏳ PENDING", "APPROVED": "✅ APPROVED",
                "REJECTED": "❌ REJECTED", "ESCALATED": "🚨 ESCALATED"}


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("Operator")
    reviewer = st.text_input("Your name", value="operator", key="reviewer")
    max_steps = st.slider("Loop guard (max steps)", 1, 10, MAX_STEPS,
                          help="Set to 2 to see the infinite-loop escalation path.")
    st.caption(f"DB: `{DB_PATH}`")
    if st.button("🔄 Refresh"):
        st.rerun()
    with st.expander("Demo data"):
        st.markdown(
            "| Order | Customer | Item | Price | Age |\n|---|---|---|---|---|\n"
            "| ORD-1001 | CUST-001 | Headphones | $59.99 | 5d → **auto-refund** |\n"
            "| ORD-1002 | CUST-002 | 4K Monitor | $249 | 10d → **>$100 → human** |\n"
            "| ORD-1003 | CUST-001 | Shoes | $89.50 | 45d → **reject** (say *damaged* → borderline → human) |\n"
            "| ORD-1004 | CUST-666 | Smart Watch | $79 | 3d → **High fraud → human** |\n"
            "| ORD-1005 | CUST-002 | Yoga Mat | $34 | 30d → **borderline → human** |\n"
            "| ORD-1006 | CUST-001 | Grinder | $45 | 60d → **reject** |"
        )

st.title("🛟 ResolveAI — Operator Console")
left, right = st.columns([1, 1.4], gap="large")


# --------------------------------------------------------------------------- #
# LEFT: customer resolution interface
# --------------------------------------------------------------------------- #
with left:
    st.subheader("1 · Customer resolution")

    for role, text in st.session_state.chat:
        with st.chat_message(role):
            st.write(text)

    with st.form("new_case", clear_on_submit=True):
        c1, c2 = st.columns(2)
        customer_id = c1.text_input("Customer ID", value="CUST-001", key="customer_id")
        order_id = c2.text_input("Order ID", value="ORD-1001", key="order_id")
        issue = st.text_area("What went wrong?", placeholder="e.g. The headphones arrived broken.", key="issue")
        submitted = st.form_submit_button("Submit refund request", type="primary", key="submit_case")

    if submitted:
        try:
            req = CaseRequest(customer_id=customer_id.strip().upper(),
                              order_id=order_id.strip().upper(),
                              issue_description=issue.strip())
        except ValidationError as e:
            st.error("Invalid input:\n\n" + "\n".join(f"- {err['loc'][0]}: {err['msg']}" for err in e.errors()))
        else:
            with st.spinner("Agent is working..."):
                tid = start_case(graph, req, max_steps=max_steps)
                registry.add(tid, req.customer_id, req.order_id, req.issue_description)
                st.session_state.latest_case_id = tid
            st.session_state.chat.append(("user", f"[{req.customer_id} / {req.order_id}] {req.issue_description}"))
            if get_pending_approval(graph, tid):
                reply = ("Thanks — your request needs a quick review by a specialist. "
                         f"We'll update you shortly. (Case `{tid}`)")
            else:
                reply = get_case_state(graph, tid).get("customer_message") or "Done."
            st.session_state.chat.append(("assistant", reply))
            st.rerun()

# --------------------------------------------------------------------------- #
# Latest decision details
# --------------------------------------------------------------------------- #
with left:
    latest_tid = st.session_state.get("latest_case_id")

    if latest_tid:
        latest = get_case_state(graph, latest_tid)

        if latest:
            st.divider()
            st.subheader("Latest Decision Details")

            order = latest.get("order_details") or {}
            policy = latest.get("policy_check") or {}
            waiting = bool(get_pending_approval(graph, latest_tid))

            status = latest.get("status", "UNKNOWN")
            risk = latest.get("fraud_risk") or "Not recorded"
            verdict = policy.get("verdict") or "Not recorded"

            if waiting:
                route = "Human review required"
            elif status == "APPROVED":
                route = "Refund approved"
            elif status == "REJECTED":
                route = "Request rejected"
            elif status == "ESCALATED":
                route = "Escalated"
            else:
                route = "In progress"

            st.caption(f"Case ID: {latest_tid}")

            a, b = st.columns(2)
            a.metric(
                "Order verification",
                "Passed" if order else "Not verified"
            )
            b.metric("Policy verdict", verdict)

            c, d = st.columns(2)
            c.metric("Fraud risk", RISK_BADGE.get(risk, risk))
            d.metric("Final decision", route)

            if policy.get("rationale"):
                st.info(f"Policy rationale: {policy['rationale']}")

            st.markdown("**Refund result**")
            st.write(f"Status: {STATUS_BADGE.get(status, status)}")
            st.write(
                f"Refund amount: ${latest.get('refund_amount') or 0:,.2f}"
            )
            st.write(
                f"Transaction ID: {latest.get('transaction_id') or 'Not generated'}"
            )

            
            with st.expander("Agent reasoning log"):
                logs = latest.get("reasoning_logs", [])

                for entry in logs:
                    st.markdown(
                        f"**{entry.get('step', '')}. "
                        f"`{entry.get('node', '')}`** — "
                        f"{entry.get('message', '')}"
                    )

                    if entry.get("tool"):
                        st.caption(
                            f"Tool: {entry['tool']} · "
                            f"{entry.get('ts', 'time unavailable')}"
                        )

                        if entry.get("output") is not None:
                            output = entry["output"]

                            if isinstance(output, (dict, list)):
                                st.json(output, expanded=False)
                            else:
                                st.code(str(output), language=None)



# --------------------------------------------------------------------------- #
# RIGHT: pending approvals
# --------------------------------------------------------------------------- #
def render_log(logs: list[dict]) -> None:
    for e in logs:
        st.markdown(f"**{e['step']}. `{e['node']}`** — {e['message']}")
        if e.get("tool"):
            st.caption(f"🔧 tool: `{e['tool']}` · {e.get('ts', 'time unavailable')}")
            out = e.get("output")
            if isinstance(out, (dict, list)):
                st.json(out, expanded=False)
            else:  # plain strings (e.g. fraud bucket "Low") are not valid JSON documents
                st.code(str(out), language=None)


def render_pending_case(tid: str, meta: dict, payload: dict) -> None:
    s = get_case_state(graph, tid)
    order = s.get("order_details") or {}
    policy = s.get("policy_check") or {}

    with st.container(border=True):
        h1, h2 = st.columns([3, 1])
        h1.markdown(f"#### Case `{tid}`")
        h2.markdown(STATUS_BADGE.get(s.get("status", "PENDING"), s.get("status", "")))
        st.caption(f"Customer **{meta['customer_id']}** · Order **{meta['order_id']}** · submitted {meta['created_at']}")
        st.markdown(f"> {meta['issue']}")

        st.markdown("**Why this needs a human**")
        for r in payload.get("reasons", []):
            st.warning(r)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Item", order.get("item_name", "—"))
        m2.metric("Refund", f"${s.get('refund_amount') or 0:,.2f}")
        m3.metric("Fraud risk", RISK_BADGE.get(s.get("fraud_risk") or "", "—"))
        m4.metric("Policy", policy.get("verdict", "—"))
        if policy:
            st.caption(f"Policy rationale: {policy.get('rationale', '')}")

        st.markdown("**Agent reasoning log**")
        render_log(s.get("reasoning_logs", []))

        notes = st.text_input("Reviewer notes (optional)", key=f"notes_{tid}")
        b1, b2, _ = st.columns([1, 1, 3])
        decision = None
        if b1.button("✅ Approve", key=f"approve_{tid}", type="primary"):
            decision = HumanDecision(approved=True, reviewer=reviewer, notes=notes)
        if b2.button("❌ Reject", key=f"reject_{tid}"):
            decision = HumanDecision(approved=False, reviewer=reviewer, notes=notes)

        if decision:
            try:
                with st.spinner("Resuming agent..."):
                    final = resume_case(graph, tid, decision)
                st.session_state.chat.append(
                    ("assistant", f"[{tid}] {final.get('customer_message', 'Case closed.')}"))
                st.rerun()
            except Exception as exc:  # surface instead of silently stranding the case
                st.error(f"Failed to resume case: {exc}")


with right:
    st.subheader("2 · Pending human approvals")
    cases = registry.recent(50)
    pending = [(c, p) for c in cases if (p := get_pending_approval(graph, c["thread_id"]))]
    if not pending:
        st.info("No cases awaiting approval. Submit a request like ORD-1002 to see one appear here.")
    for meta, payload in pending:
        render_pending_case(meta["thread_id"], meta, payload)


# --------------------------------------------------------------------------- #
# BOTTOM: history
# --------------------------------------------------------------------------- #
st.divider()
st.subheader("3 · Recent cases")
rows = []
for c in registry.recent(25):
    s = get_case_state(graph, c["thread_id"])
    waiting = bool(get_pending_approval(graph, c["thread_id"]))
    rows.append({
        "case": c["thread_id"], "customer": c["customer_id"], "order": c["order_id"],
        "status": STATUS_BADGE.get(s.get("status", ""), "") + (" (awaiting human)" if waiting else ""),
        "refund": s.get("refund_amount"), "txn": s.get("transaction_id"), "steps": s.get("step_count"),
    })
if rows:
    st.dataframe(rows, hide_index=True)
else:
    st.caption("No cases yet.")
