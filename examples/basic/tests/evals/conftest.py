from __future__ import annotations

import pytest

from kensa.tracing import record_tool_call


@pytest.fixture
def kensa_run():
    def _run(case):
        with record_tool_call("lookup_customer"):
            text = str(case.input).lower()
            found_order = "order #" in text or "order id:" in text
        if found_order:
            with record_tool_call("issue_refund"):
                return {"message": "Refund issued."}
        return {"message": "I need order history before issuing a refund."}

    return _run
