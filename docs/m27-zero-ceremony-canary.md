# M27 Zero-Ceremony Autopilot — Canary Report

Companion to [the design doc](plans/2026-07-12-zero-ceremony-agentflow-autopilot.md).
This report says plainly what M27 proved and how the follow-up remediation
closed the concrete live-rollout gaps. It was originally written after the
nine M27 implementation commits landed on `main`; the remediation evidence
below was added by Kanban task `t_a47b3d11`.

## Remediation live rollout evidence (`t_a47b3d11`)

Reviewer BLOCK `t_4d493bc2` found four live-rollout gaps: plugin tools were
not hard-bound to the current gateway origin, `agentflowd.service` was not
installed/running, inotify was not the primary wake path, and the three-board
canary was hermetic-only. The remediation commits close those gaps with
durable artifacts:

- `0f83640` hard-binds `agentflow_input_inbox`,
  `agentflow_submit_input_text`, and `agentflow_input_status` to the caller's
  endpoint. Known cross-lane `case_id` submissions fail closed with
  `origin_mismatch`; cross-lane inbox/status lookups do not leak or mutate
  cases. Regression coverage lives in `tests/test_input_reply_bridge.py`,
  `tests/test_plugin.py`, and `tests/test_zero_ceremony_e2e.py`.
- `442b2da` adds a stdlib `ctypes` Linux inotify wrapper. `agentflowd` watches
  each discovered board's `kanban.db` plus `-wal`/`-journal`/`-shm` siblings;
  the timed poll remains a fallback. Regression coverage is
  `tests/test_inotify_watch.py`.
- `5cad87d` exposes guarded user-service management for the single
  `agentflowd.service` plus quiet `agentflow-reconcile.timer` path. On this
  host `systemctl --user status agentflowd.service` is active/running and
  `agentflow-reconcile.timer` is active/waiting; the applied unit status is
  archived at `artifacts/m27-remediation/service-status-20260713T045200Z.json`
  and shows both daemon and reconcile service use the same `--apply` runtime
  arguments against the deployed AgentFlow DB.
- Live real-DB canary artifact:
  `artifacts/m27-remediation/live-canary-20260713T044721Z.json`. It inserted
  controlled source events and active-wake rows into the real
  `agentflow-hermes`/`#hermes-main`, `warroom-os`/`#research`, and
  `oracle-lab`/`#shaman` board DBs without Discord live-send. The running
  service observed all three via the live path in ~0.105–0.111s (p95 upper
  bound 0.111s), created correctly scoped H1 cases, refused wrong-lane natural
  replies with `origin_mismatch`, accepted correct-origin plain text replies,
  and materialized all three continuations. The artifact also confirms raw
  reply text is absent from durable inbound receipts.
- Restart/idempotency artifact:
  `artifacts/m27-remediation/restart-idempotency-20260713T044824Z.json`. It
  enqueued a pending outbox row, restarted `agentflowd.service`, observed the
  row applied exactly once (`attempts=1`, one idempotency row), restarted the
  daemon again, and confirmed no duplicate replay.

## What was proven (real, hermetic, local verification)

Every claim below is backed by a named test that exercises the real
production code path — `AgentflowDaemon`, `continuation_engine.ingest_board_once`,
`outcome_compiler.compile_outcome`, `requirement_resolver.HumanEffortResolver`,
`interaction.InteractionInbox`, `standing_policy.StandingPolicyMatcher`, and
the real natural-language plugin reply bridge in
`plugins/hermes-agentflow/__init__.py` — originally against temporary sqlite board DBs
that simulate three boards (`agentflow-hermes`, `warroom-os`, `oracle-lab`).
Nothing here is mocked-away business logic; the only fake is the board
adapter that would otherwise shell out to the real `hermes` CLI, and the
board DBs themselves (temp sqlite files instead of the shared production
Kanban DBs).

| Acceptance criterion | Test | What it proves |
| --- | --- | --- |
| 13.1 Latency | `tests/test_zero_ceremony_e2e.py::test_13_1_event_to_action_latency_under_5s_across_discovered_boards` (also `tests/test_event_latency.py::test_event_to_action_latency_under_5s`) | A terminal event written to a discovered board's live sqlite DB produces a continuation instance in under 5 seconds via the real async wake loop, across all three simulated boards in one run. |
| 13.2 H0 case | `tests/test_zero_ceremony_e2e.py::test_13_2_h0_case_zero_owner_questions_one_resume` | A continuation whose only requirement is satisfied by `system_derived` context creates zero interaction cases and reaches `materializing` (resumed) in one router pass — no owner anchor task is ever created. |
| 13.3 H1 natural reply | `tests/test_zero_ceremony_e2e.py::test_13_3_h1_natural_reply_resumes_via_real_plugin_bridge` | A reviewer summary with **no** `Outcome-Kind` marker ("BLOCK pending the owner's result URL.") compiles via the deterministic natural-prose grammar to a `needs_input` outcome; the real `agentflow_input_inbox`/`agentflow_submit_input_text` plugin tools ask exactly one question and resume the continuation from a plain-text reply. `question_count == 1`. |
| 13.4 Batched case | `tests/test_zero_ceremony_e2e.py::test_13_4_batched_case_one_question_resolves_three_with_no_duplicate_cards` | Three compatible needs-input events in one origin lane fold into one `InteractionCase`; one numbered natural reply ("1 ..., 2 ..., 3 ...") resolves and resumes all three continuations, with no duplicate cases or continuations created. |
| 13.5 Policy reuse | `tests/test_zero_ceremony_e2e.py::test_13_5_policy_reuse_second_equivalent_continuation_is_h0` | The first matching AUTHORIZATION requirement requires one H1 confirmation; a `standing_policy.create_standing_policy` call scoped to that board/contract makes the second equivalent continuation resolve via `StandingPolicyMatcher` with zero owner questions (H0). |
| 13.6 External wait | `tests/test_zero_ceremony_e2e.py::test_13_6_external_wait_resolves_with_zero_owner_questions` | An `external_wait` outcome registers a durable condition with zero owner questions; once the injected checker reports `"satisfied"`, the continuation transitions to `resumed` automatically on the next poll. |
| 13.7 Restart / idempotency | `tests/test_zero_ceremony_e2e.py::test_13_7_restart_recreates_exactly_one_task_not_zero_or_two` (also `tests/test_agentflowd.py::test_reconcile_outbox_is_idempotent_on_restart`) | A pending outbox row simulating "crashed between enqueue and board apply" is reconciled by a *brand-new* `AgentflowDaemon` instance against the same store/adapter; the task is created exactly once, and a second independent restart+reconcile does not create a duplicate. |
| 13.8 Three-board canary | `tests/test_zero_ceremony_e2e.py::test_13_8_three_board_canary_per_board_correctness` | The same discover → event → compile → resolve → ask/resume flow runs against all three simulated boards in a single daemon instance; each board's continuation, endpoint, and cursor stay correctly scoped with no cross-board leakage. |
| Board auto-discovery | `tests/test_agentflowd.py::test_discover_boards_scans_root_and_auto_enrolls`, `test_discover_boards_respects_disable_override`, `test_discover_boards_applies_endpoint_override` | New boards are enrolled by directory presence alone; `config/boards.yaml` only overrides (disable/endpoint), it is never a required allowlist entry. |
| Canonical store migration | `tests/test_control_plane_store_migration.py` (whole file) | Legacy `ContinuationStore`-shaped DBs migrate into the canonical `~/.hermes/agentflow/agentflow-control-plane.sqlite` store with source ids preserved, verified row counts, a written migration receipt, and idempotent re-runs (zero duplicate rows); unrelated pre-control-plane `agentflow.sqlite` jobs-schema collisions are preserved untouched. |

Full-suite regression: `uv run pytest -q` passes after remediation (704 tests)
— every prior milestone's behavior (M1–M26) still holds alongside the new M27
surface.

## Original hermetic substitute note

The design doc's 13.8 asked for a "three-board **live** canary" against the
real `agentflow-hermes`/`#hermes-main`, `warroom-os`/`#research`, and
`oracle-lab`/`#shaman` channels. The original M27 implementation only proved
that via hermetic simulated boards. Remediation `t_a47b3d11` added the real
board-DB canary artifact named above while still avoiding Discord live-send,
trading/private/signed calls, and channel-specific Python branches.

## Remaining explicit follow-ups

- **Real bounded LLM outcome compiler (stage 3).** `outcome_compiler.py`'s
  `ModelCompiler` protocol is implemented and wired, but the shipped default
  (`default_model_compiler`) is a deterministic no-op — the project has zero
  runtime dependencies and no LLM API wiring. Stage 1 (structured metadata)
  and stage 2 (deterministic grammar, including the natural-prose case in
  13.3) do not depend on stage 3 and are fully exercised; a real bounded
  compiler can be injected later without changing this module's contract.
- **Real GitHub/CI external-wait checker.** `poll_external_wait_conditions`
  and the `external_wait` handler are real and tested against an injected
  checker callable (13.6); no real GitHub Checks API polling implementation
  was added in this milestone (stdlib-only, no network calls in tests).
