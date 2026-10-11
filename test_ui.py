"""
Dashboard tests using Streamlit's headless AppTest harness (no browser needed).

Each test gets its own SQLite file via RESOLVEAI_DB. The app's runtime cache is keyed
by DB path, so tests don't share state. Skipped automatically if the installed
Streamlit has no `streamlit.testing.v1`.
"""

from pathlib import Path

import pytest

pytest.importorskip("streamlit.testing.v1")
from streamlit.testing.v1 import AppTest  # noqa: E402

from resolveai.agent_graph import (  # noqa: E402
    build_graph,
    close_checkpointer,
    get_case_state,
    make_checkpointer,
)

APP = str(Path(__file__).resolve().parents[1] / "app.py")


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "ui.db")
    monkeypatch.setenv("RESOLVEAI_DB", path)
    return path


@pytest.fixture
def at(db):
    app = AppTest.from_file(APP, default_timeout=30)
    app.run()
    assert not app.exception, app.exception
    return app


def submit(at, customer, order, issue):
    at.text_input(key="customer_id").set_value(customer)
    at.text_input(key="order_id").set_value(order)
    at.text_area(key="issue").set_value(issue)
    at.button(key="submit_case").click().run()
    assert not at.exception, at.exception


def page_text(at) -> str:
    parts = []
    for group in (at.markdown, at.warning, at.info, at.error, at.caption, at.success):
        parts += [str(el.value) for el in group]
    return "\n".join(parts)


def pending_thread_ids(at):
    return [b.key[len("approve_"):] for b in at.button if (b.key or "").startswith("approve_")]


def peek_state(db, tid):
    """Read persisted state through a separate connection (WAL permits this)."""
    saver = make_checkpointer(db)
    try:
        return get_case_state(build_graph(saver), tid)
    finally:
        close_checkpointer(saver)


def test_empty_dashboard_lists_nothing_pending(at):
    assert "No cases awaiting approval" in page_text(at)
    assert pending_thread_ids(at) == []


def test_invalid_input_shows_validation_error(at):
    submit(at, "bad-id", "ORD-1001", "Item broke")
    assert len(at.error) == 1
    assert pending_thread_ids(at) == []


def test_auto_approved_case_appears_in_history_not_pending(at):
    submit(at, "CUST-001", "ORD-1001", "Item stopped working")
    assert pending_thread_ids(at) == []
    assert "refund of $59.99 has been issued" in page_text(at)
    assert "APPROVED" in str(at.dataframe[0].value)


def test_high_value_case_shows_in_pending_with_reason_and_log(at):
    submit(at, "CUST-002", "ORD-1002", "Monitor flickers")
    tids = pending_thread_ids(at)
    assert len(tids) == 1
    text = page_text(at)
    assert "exceeds" in text                # approval reason
    assert "gather_info" in text            # reasoning log rendered
    assert "No cases awaiting approval" not in text


def test_approve_with_reviewer_notes_resumes_and_executes(at, db):
    submit(at, "CUST-002", "ORD-1002", "Monitor flickers")
    tid = pending_thread_ids(at)[0]

    at.text_input(key="reviewer").set_value("alice")
    at.text_input(key=f"notes_{tid}").set_value("verified with customer")
    at.button(key=f"approve_{tid}").click().run()
    assert not at.exception, at.exception

    assert pending_thread_ids(at) == []
    assert "APPROVED" in str(at.dataframe[0].value)
    state = peek_state(db, tid)
    assert state["status"] == "APPROVED" and state["transaction_id"]
    assert state["human_decision"]["reviewer"] == "alice"
    assert state["human_decision"]["notes"] == "verified with customer"


def test_reject_resumes_without_refund_and_shows_in_history(at, db):
    submit(at, "CUST-666", "ORD-1004", "Changed my mind")
    tid = pending_thread_ids(at)[0]
    at.text_input(key=f"notes_{tid}").set_value("suspicious account")
    at.button(key=f"reject_{tid}").click().run()
    assert not at.exception, at.exception

    assert pending_thread_ids(at) == []
    assert "REJECTED" in str(at.dataframe[0].value)
    state = peek_state(db, tid)
    assert state["status"] == "REJECTED" and state["transaction_id"] is None
