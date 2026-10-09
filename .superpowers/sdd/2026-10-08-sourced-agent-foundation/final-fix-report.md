# Final review repair report

Date: 2026-10-09. Branch: `codex/agent-capability-design`. Review baseline: `da616bb495e24491c31afec30a8dff045ebd0ebc`. Scope: final-review I1-I4 and M1 only; earlier four implementation tasks were retained.

## Fixes and evidence

- **I1:** collector contract/prompt now separates train `passengers` from hotel `guests`, including differing explicit counts and shared confirmed party counts. Unknown train count remains the existing one-passenger query default; hotel-only guests never override it. Connection tests execute actual collector, sourced agent, Provider and budget guard: two passengers at 500 produce 1000; unknown passengers and hotel-only guests produce 500; two train passengers and one hotel guest reach distinct queries.
- **I2:** confirmed itinerary start/end dates expand into GuideQuery visit_dates including both endpoints; start alone yields one day, unknown dates yield none. Explicit visit_dates still wins. Actual collector→guide→Provider tests capture single-day, three-day and unknown-date requests. The parent-approved semantics are recorded in the design and collector skill, retaining exclusive hotel check_out semantics.
- **I3:** domain error/partial/needs_input/unavailable survive orchestration wrapping. Aggregate mixed success/incomplete results are partial_failure; uniform needs_input/unavailable states remain explicit. Standard sourced agent timeout handling is exercised with an actual failing fake Provider alongside a successful guide Provider. EDD checks and failed_agents metrics also examine business status, including historical outer-success records.
- **I4:** after model sorting, priorities become compact integer ranks before dependency increments. Nonfinite floats use the default rank. Tests cover 1e20, Infinity, -Infinity and NaN, both normalized batches and actual previous_results delivery: collector first, train/hotel same batch, planner last.
- **M1:** orchestrator passes the explicitly requested domains to collector; hotel required fields follow that set. Standalone collector falls back to stated hotel intent. Pure train/guide queries remove hotel missing fields, while hotel requests retain them.
- **CRLF checks:** the five existing files identified by the review retain their Windows line endings. Scoped `.gitattributes` `whitespace=cr-at-eol` declares those endings legitimate; trailing spaces remain checked. This avoids rewriting baseline file contents merely to change line endings. Branch comparison is checked, not just the empty workspace.

## TDD and commands

All Python commands use `../../.venv/Scripts/python.exe`; no live LLM or external Provider was called.

1. `-m pytest -q tests/test_final_fix_connections.py` before repair: **12 failed, 2 passed** after removing an overly early prompt assertion from the fixture. Correct failures reproduced extreme scheduling, guide dates, standard domain states and hotel-only missing fields. The initial 14-failure fixture run was not relied upon as individual behavior evidence.
2. EDD historical-success fixture was corrected to include required run_seq/user/session metadata. With the EDD inner-status repair absent, `-m pytest -q tests/test_final_fix_connections.py -k edd`: **4 failed**, all because execution_success was incorrectly true. After adding failed_agents assertions and before repairing that metric: **4 failed**, all because failed_agents omitted train_search. The temporary fixture errors are not counted as product regressions.
3. Final focused command: `-m pytest -q tests/test_final_fix_connections.py tests/test_sourced_routing.py tests/test_sourced_evals.py`: **42 passed**.
4. Complete offline suite: `-m pytest -q tests --ignore=tests/test_intention_agent.py`: **154 passed, 5 skipped, 1 warning**. The excluded intention test is the established online test; skipped tests and existing dashscope deprecation warning are unchanged gates.
5. `-m compileall -q agents travel_data .claude/skills cli.py`: exit **0**.
6. `git diff --check da616bb495e24491c31afec30a8dff045ebd0ebc` and `git diff --check`: exit **0**, including branch-to-working-tree changes. Post-commit `git diff --check da616bb495e24491c31afec30a8dff045ebd0ebc HEAD`: exit **0**. A temporary trailing-space probe on the scoped orchestration file returned **2** and identified the added trailing space; the probe was restored before staging, proving trailing-space protection remains enabled.

## Self-review and limits

Compared implementation against the approved design and first-stage plan: confirmed fields reach domain queries; selected sourced train price is multiplied by the query passenger count; date semantics are explicit; failures preserve siblings; dependency phases cannot collapse under model float arithmetic. CLI already displays domain business data before its legacy success-only filter, so explicit wrapper statuses do not suppress domain output.

The repository has no initialized OpenSpec verify workflow. This report documents manual consistency checking; it does not claim OpenSpec verify was run. Real platform adapters, authorizations and live source verification remain subsequent stages. Collector extraction remains model-dependent; offline tests verify its prompt contract and normal structured outputs, not a live model's extraction accuracy. Mixed incomplete results retain the existing partial_failure aggregate vocabulary. No unresolved Important finding is known after self-review; independent re-review remains the parent's next gate.
