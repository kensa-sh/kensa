# Triage agent

This is the eval-bootstrap target agent for this repo. Kensa's own repository is a
testing library with no user-facing conversational agent of its own, so it has
nothing for `tests/evals/conftest.py::kensa_run` to call. This small on-call
incident triage agent exists to give Kensa's eval lifecycle a real production
boundary to wire into and test against — it is not a product feature.

`TriageAgent.run(messages)` takes an alert as a conversation and, via real model
calls through `any-llm-sdk`, decides whether to page on-call after checking service
status and error-rate tools. The tools (`check_service_status`, `query_error_rate`,
`page_oncall`) are in-memory stubs over a small fixed dataset; `page_oncall` never
sends a real page.
