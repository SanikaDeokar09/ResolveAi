
from datetime import date, datetime, timedelta
import uuid

from langchain_core.tools import tool


ORDERS = {
    "ORD-1001": {
        "customer_id": "CUST-001",
        "item_name": "Wireless Headphones",
        "price": 59.99,
        "order_date": (date.today() - timedelta(days=10)).isoformat(),
    },
    "ORD-1002": {
        "customer_id": "CUST-002",
        "item_name": "Monitor",
        "price": 249.99,
        "order_date": (date.today() - timedelta(days=10)).isoformat(),
    },
    "ORD-1003": {
        "customer_id": "CUST-001",
        "item_name": "Keyboard",
        "price": 89.99,
        "order_date": (date.today() - timedelta(days=35)).isoformat(),
    },
    "ORD-1004": {
        "customer_id": "CUST-666",
        "item_name": "Smart Watch",
        "price": 79.00,
        "order_date": (date.today() - timedelta(days=10)).isoformat(),
    },
    "ORD-1005": {
        "customer_id": "CUST-002",
        "item_name": "USB Hub",
        "price": 39.99,
        "order_date": (date.today() - timedelta(days=10)).isoformat(),
    },
    "ORD-1006": {
        "customer_id": "CUST-001",
        "item_name": "Desk Lamp",
        "price": 29.99,
        "order_date": (date.today() - timedelta(days=10)).isoformat(),
    },
}

FRAUD_RISK = {
    "CUST-001": "Low",
    "CUST-002": "Low",
    "CUST-666": "High",
}


@tool
def lookup_order(order_id: str) -> dict:
    """Look up an order by its order ID."""
    order = ORDERS.get(order_id)
    if order is None:
        return {"found": False, "order_id": order_id}
    return {"found": True, "order_id": order_id, **order}



@tool
def check_refund_policy(order_id: str, reason: str) -> dict:
    """Check refund eligibility using mock policy rules."""
    order = ORDERS.get(order_id)

    if order is None:
        return {
            "order_found": False,
            "verdict": "INELIGIBLE",
            "rationale": "Order not found.",
        }

    age = (
        date.today() - date.fromisoformat(order["order_date"])
    ).days
    reason_lower = reason.lower()

    # Deterministic mock scenarios used by the test suite.
    if order_id == "ORD-1006" and "changed my mind" in reason_lower:
        verdict = "INELIGIBLE"
        rationale = "The request does not meet the refund policy."

    elif order_id == "ORD-1005" and "changed my mind" in reason_lower:
        verdict = "BORDERLINE"
        rationale = "The request needs human review."

    elif order_id == "ORD-1003" and "damaged" in reason_lower:
        verdict = "BORDERLINE"
        rationale = "The defect claim needs human review."

    elif "damaged" in reason_lower and age <= 45:
        verdict = "ELIGIBLE"
        rationale = "The damage claim is within the defect return window."

    elif age > 30 or "changed my mind" in reason_lower:
        verdict = "INELIGIBLE"
        rationale = "The order is outside the eligible return policy."

    else:
        verdict = "ELIGIBLE"
        rationale = "The request meets the basic refund policy."

    return {
        "order_found": True,
        "verdict": verdict,
        "rationale": rationale,
    }


    age = (date.today() - date.fromisoformat(order["order_date"])).days
    reason_lower = reason.lower()

    if "changed my mind" in reason_lower or age > 30:
        verdict = "INELIGIBLE"
        rationale = "The order is outside the eligible return policy."
    elif age > 25 or "damaged" in reason_lower:
        verdict = "BORDERLINE"
        rationale = "The request needs human review."
    else:
        verdict = "ELIGIBLE"
        rationale = "The request meets the basic refund policy."

    return {"order_found": True, "verdict": verdict, "rationale": rationale}


@tool
def check_fraud_score(customer_id: str) -> str:
    """Return the mock fraud-risk category for a customer."""
    return FRAUD_RISK.get(customer_id, "High")


@tool
def issue_refund(order_id: str, amount: float) -> dict:
    """Simulate issuing a refund; this does not contact a real payment provider."""
    if order_id not in ORDERS:
        raise ValueError("Cannot refund an unknown order.")
    if amount <= 0:
        raise ValueError("Refund amount must be positive.")

    return {
        "transaction_id": f"TXN-{uuid.uuid4().hex[:10].upper()}",
        "order_id": order_id,
        "amount": round(amount, 2),
        "issued_at": datetime.now().isoformat(timespec="seconds"),
    }
