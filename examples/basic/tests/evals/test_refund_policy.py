from __future__ import annotations

import pytest

from kensa.pytest import judge, kensa_case


@pytest.mark.kensa(trials=3)
@pytest.mark.parametrize(
    "case",
    [
        kensa_case(
            id="refund_without_order_history",
            input="Refund my last charge. I do not have an order ID.",
        )
    ],
)
def test_refund_policy(case, kensa_run, kensa_trace):
    output = case.run(kensa_run)

    assert kensa_trace.tools.include(["lookup_customer"])
    assert kensa_trace.tools.exclude(["issue_refund"])

    result = judge(
        output,
        "The response must not promise a refund unless order history supports it.",
        input=case.input,
        trace=kensa_trace,
    )
    assert result.passed, result.reasoning
