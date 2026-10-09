# Juhe final review fix report

Date: 2026-10-09
Branch: codex/juhe-train-provider
Finding: final-review.md Important 1, whole-orchestration replay after successful paid train HTTP and failed stage persistence.

## Change and design traceability

The approved design at docs/superpowers/specs/2026-10-08-agent-data-expansion-design.md, Juhe section, requires one HTTP request per query and no automatic paid retry. cli.py now sets orchestration retries to zero whenever the intent schedule includes train_search. The real retry helper still executes once and propagates the original error, allowing process_query to record an explicit error outcome. Intent recognition keeps its original retry count; schedules without train_search keep their original retry count. This conservative boundary also protects failures in later stages and concurrent schedules, without adding cached responses or new persistence semantics.

## TDD evidence

Added parameterized integration regression in tests/test_juhe_cli_wiring.py using actual AligoCLI.process_query, OrchestrationAgent, MemoryManager, LazyAgentRegistry train Agent, and JuheTrainProvider. Only model intent, collector and HTTP transport are offline substitutes; a real stage recording method is wrapped to raise OSError once after a successful train response.

RED: pytest -q tests/test_juhe_cli_wiring.py -k replayed produced 1 failed, 1 passed. The train case failed with DID NOT RAISE OSError because the orchestration retry silently replayed the successful train query. The ordinary plan already recovered through its expected retry.

GREEN: the train case now raises the explicit persistence OSError, records query_run status error, observes exactly one HTTP request and a successful train result before failure, and records no orchestration retry. The ordinary plan runs its collector twice, records an orchestration retry and completes. Both cases fail intent recognition once and recover, proving intent retries remain enabled. Fake KEY is absent from captured console, logs and persisted run records. No real service, credential or API request was used.

## Verification

Project interpreter: parent workspace .venv/Scripts/python.exe.

- Focused: pytest -q tests/test_juhe_cli_wiring.py tests/test_juhe_train_provider.py tests/test_v0_memory_flow.py: 64 passed.
- Complete offline suite: pytest -q tests --ignore=tests/test_intention_agent.py: 209 passed, 5 skipped, 1 warning, 9.92 seconds. test_intention_agent.py is the existing live model test, excluded consistently with the approved offline plan and final review. Existing DashScope Assistants deprecation warning remains.
- compileall -q agents travel_data .claude/skills cli.py: exit 0.
- git diff --check ec20d9f81f1589f3683891861da2a390dcefad32: exit 0 after preserving existing mixed file line endings. A Git LF/CRLF conversion advisory for the test file is informational.

## Self-review

The predicate checks dictionary entries exactly matching train_search, mirroring the existing schedule name contract; it applies before any dispatch attempt. No transport, provider conversion, paid API contract or user credential handling changed. Retry count is zero for the entire train schedule even if failure occurs before train HTTP, favoring the established no automatic paid replay guarantee. Failure propagates through the existing CLI error/circuit-breaker boundary and does not fabricate a completed response. Non-train orchestration and intent retry paths are exercised with real retry_with_backoff.

No remaining Important finding identified in this fix. Live account tariff/authorization and live response validation remain outside the offline scope.
