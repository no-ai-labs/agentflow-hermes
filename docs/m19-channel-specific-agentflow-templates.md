# M19 channel-specific AgentFlow templates

Kanban task: `t_59f1c165`
Origin/return_to: Discord Devhub / #hermes-main
Status: design artifact only — no production code, live system, money, Discord scan, or board mutation in this task.

## Route evidence

- Native Claude Code route was available and used for the design consultation:
  `claude --model opus -p --max-turns 12 --output-format json < m19-opus-design-prompt.md`.
- Supervisor preflight showed `claude` at `/home/duckran/.hermes/profiles/ccsupervisor/home/bin/claude`, Claude Code `2.1.204`, `claude auth status --text` = Claude Max account, and `tmux` at `/usr/bin/tmux`.
- The Claude Code JSON result reported `modelUsage` key `claude-opus-4-8`; no OpenRouter/Kimi/Moonshot route was used.
- Supervisor independently inspected the repo files and the live channel configs before writing this doc.

## 1. Goal and stance

M19 extends the M17/M18 roadmap GO autopromoter from the current generic `impl -> review -> fanin` graph into channel-specific templates for:

- `#research` / `warroom-os`: `scout -> evidence -> scorecard -> review -> brief`
- `#shaman` / `oracle-lab`: `design -> impl -> browser_e2e -> review -> fanin`

The purpose is not a broad policy platform. M19 should stay a vertical template-rendering slice: named presets, deterministic task bodies, exact continuation markers, tests, and a canary through the existing `roadmap promote|watch` path.

Non-goals:

- no Discord channel direct scan;
- no live send, active wake, restart, trading, money, or system side effects;
- no free-text task synthesis;
- no worker-specific policy sprawl beyond a small curated preset registry.

## 2. Baseline findings

The existing graph path is already mostly data-driven:

```text
repo/channel YAML
  transitions[*].slice_template
    -> roadmap_config.build_registry()
    -> RoadmapTransition.slice_template
    -> roadmap._build_plan()
    -> GraphIntentCandidate chain
    -> RealKanbanGraphAdapter create calls when double-armed
```

The main blockers are narrow and implementable:

| Blocker | Current behavior | Required M19 fix |
| --- | --- | --- |
| Continuation markers are tied to literal `fanin` | `roadmap._build_plan()` injects `Roadmap-Transition`, `Next-Slice`, and `Auto-Continue: false` only when `kind == "fanin"`. | Use the template role `terminal` rather than the string `fanin`; include a full final ACK skeleton. |
| Task profile is name-classified | `roadmap._apply_task_profile()` treats `impl` as implementation, `review` as review, and every other kind as fan-in/ACK. | Use role metadata: work/review/terminal. Otherwise `scout`, `evidence`, and `scorecard` would accidentally become ACK tasks. |
| Scaffold is generic | `roadmap_register.render_roadmap_config()` always emits `impl/review/fanin`. | Add a template selector that renders the curated channel presets while preserving the legacy default. |
| Task bodies lack lane anchors | Generated bodies mostly carry policy refs and minimal markers. | Add standing goal anchors and lane-specific expectations so #research and #shaman do not drift. |
| Final reports rely on operator memory | Review/fan-in workers must remember exact continuation markers. | Generate the marker instructions into review and terminal card bodies. |

## 3. Curated preset registry

Add a small module, likely `src/agentflow_hermes/roadmap_templates.py`, with a closed set of presets. Each preset owns ordered kinds, per-kind role, lane anchor text, and body snippets. Unknown preset names fail closed.

Suggested core model:

```python
@dataclass(frozen=True)
class RoadmapTemplateStep:
    kind: str                  # scout, evidence, scorecard, review, brief, ...
    role: str                  # work | review | terminal
    assignee_role: str = ""    # impl | review | ack, mapped to existing config assignees
    body_anchor: str = ""      # deterministic standing guidance

@dataclass(frozen=True)
class RoadmapTemplatePreset:
    name: str
    lane: str                  # hermes-main | research | shaman
    slice_template: tuple[str, ...]
    steps: dict[str, RoadmapTemplateStep]
    goal_anchor: str
```

Initial presets:

1. `impl-review-fanin` (legacy)
   - sequence: `impl`, `review`, `fanin`
   - roles: work, review, terminal
   - purpose: current #hermes-main behavior and backward compatibility

2. `research-loop`
   - sequence: `scout`, `evidence`, `scorecard`, `review`, `brief`
   - roles:
     - `scout`: work — find candidate topic/source/event, define question and scope
     - `evidence`: work — collect citations, primary-source links, numbers, and contradictions
     - `scorecard`: work — rank confidence/impact/actionability and identify red flags
     - `review`: review — fail-closed evidence review, source quality, lane fit
     - `brief`: terminal — concise final research brief with ACK/continuation markers
   - standing anchor: `#research / warroom-os exists to turn signals into sourced, scored, operator-useful research briefs; do not drift into generic implementation tasks or unverified speculation.`

3. `shaman-loop`
   - sequence: `design`, `impl`, `browser_e2e`, `review`, `fanin`
   - roles:
     - `design`: work — artifact UX/IA/content design for oracle-lab/wiki/KB experiences
     - `impl`: work — implement the bounded artifact/config/template slice
     - `browser_e2e`: work — run browser/user-flow smoke for generated or UI artifacts
     - `review`: review — check drift, safety, raw-envelope leaks, e2e evidence
     - `fanin`: terminal — final ACK with runtime smoke and next-slice markers
   - standing anchor: `#shaman / oracle-lab exists to build accumulated oracle/wiki/KB artifacts with browser-verifiable UX; do not drift into generic guardrail work or unsupported esoteric claims.`

Preset invariants:

- exactly one `review` role and exactly one `terminal` role;
- terminal role must be the final step;
- all step kinds must be identifier-safe and present in `slice_template`;
- preset-defined sequence wins; config mismatch is an error rather than a silent override;
- default/empty preset resolves to legacy `impl-review-fanin`.

This is the anti-sprawl boundary: M19 adds three named presets, not arbitrary per-channel DSL behavior.

## 4. YAML shape and migration

Current configs remain valid. Add optional transition-level fields:

```yaml
transitions:
  research.default.scout_evidence_scorecard_review_brief:
    roadmap_id: research.roadmap
    from_slice: research-current
    to_slice: research-next
    template_preset: research-loop
    # optional explicit sequence for readability; if present, must match preset
    slice_template:
      - scout
      - evidence
      - scorecard
      - review
      - brief
    goal_anchor: "#research / warroom-os: sourced, scored research briefs; no generic guardrail sprawl."
    policy_refs:
      - design_opus
      - implementation_default
    max_chain_depth: 3
    version: template-v2
```

```yaml
transitions:
  shaman.default.design_impl_browser_review_fanin:
    roadmap_id: shaman.roadmap
    from_slice: shaman-current
    to_slice: shaman-next
    template_preset: shaman-loop
    slice_template:
      - design
      - impl
      - browser_e2e
      - review
      - fanin
    goal_anchor: "#shaman / oracle-lab: accumulated oracle/wiki/KB artifacts with browser-verifiable UX."
    policy_refs:
      - design_opus
      - implementation_default
    max_chain_depth: 3
    version: template-v2
```

Dataclass evolution:

```python
@dataclass(frozen=True)
class RoadmapTransition:
    transition_id: str
    roadmap_id: str
    from_slice: str
    to_slice: str
    slice_template: tuple[str, ...]
    policy_refs: tuple[str, ...]
    max_chain_depth: int = 3
    version: str = ""
    template_preset: str = ""  # new, optional; empty means legacy
    goal_anchor: str = ""      # new, optional; preset can provide default
```

Backward compatibility rules:

- Existing `agentflow-roadmap.yaml` and current research/shaman YAMLs load unchanged because `template_preset` and `goal_anchor` default to empty.
- If `template_preset` is omitted, role inference uses legacy fallback: `review` kind = review role, final kind = terminal role, all other kinds = work role. This preserves `impl/review/fanin` behavior and prevents arbitrary non-impl kinds from becoming ACK tasks.
- If `template_preset` is present and `slice_template` is omitted, derive sequence from the preset.
- If both are present, require exact sequence match. A mismatch means malformed config and no board write.
- Keep `same_board_only`, `expected_origin`, `expected_return_to`, `trusted_assignees`, double apply gate (`apply_mode` + CLI `--apply`), and receipts idempotency unchanged.

Recommended channel config migration:

- `/home/duckran/.hermes/agentflow/roadmaps/research-roadmap.yaml`
  - rename transition from `research.default.impl_review` to `research.default.scout_evidence_scorecard_review_brief` or introduce it alongside the old one during canary;
  - `allowed_transitions` should name only the active canary transition;
  - set `template_preset: research-loop` and `version: template-v2`.
- `/home/duckran/.hermes/agentflow/roadmaps/shaman-roadmap.yaml`
  - rename transition from `shaman.default.impl_review` to `shaman.default.design_impl_browser_review_fanin` or introduce it alongside the old one during canary;
  - set `template_preset: shaman-loop` and `version: template-v2`.
- `/home/duckran/dev/agentflow-hermes/agentflow-roadmap.yaml`
  - keep legacy preset as the control lane;
  - add the next implementation transition `m18->m19.channel_templates` after this design review GO.

## 5. Generated body contract

Every generated card body should be deterministic and template-derived. It must not copy arbitrary free text from a final GO source beyond already sanitized refs.

Common body sections:

```text
Policy refs:
- design_opus
- implementation_default

Roadmap context:
Lane: <#research|#shaman|#hermes-main>/<board>
Goal anchor: <preset or transition goal_anchor>
Roadmap-Transition: <transition_id>
Slice: <to_slice>
Step: <kind>
Role: <work|review|terminal>
Source-Final-Ref: <safe source ref>

Step objective:
<template step-specific objective>
```

Review cards should additionally include exact continuation-marker instructions:

```text
Review output requirements:
- Emit Verdict: GO or Verdict: BLOCK/NEED_MORE.
- If GO, include the continuation markers exactly so the terminal step can preserve them:
  Review-Edge: verified
  Parent-GO: verified
  Roadmap-Transition: <transition_id>
  Next-Slice: <to_slice>
- Do not set Auto-Continue: true unless this is the final terminal ACK and the next transition is intentionally approved.
```

Terminal cards (`brief` for research, `fanin` for shaman and legacy) should include the full final ACK schema:

```text
Final ACK schema (fill all fields; use none/n/a explicitly when absent):
Verdict: GO|BLOCK|NEED_MORE
Slice: <to_slice>
Evidence: <sources/artifacts or n/a>
Commit: <sha or none>
Tests: <commands/results or n/a>
Runtime smoke: <command/result or n/a>
Next: <human-readable next action>
Roadmap-Transition: <transition_id>
Next-Slice: <to_slice>
Review-Edge: verified|missing
ACK-Edge: verified|missing
Parent-GO: verified|missing
Auto-Continue: false
Origin/return_to: <expected_origin>
Return-To: <expected_return_to>
Subscription-Status: verified
Policy-Resolution-Ref: <central policy/config ref>
```

Important: body templates may instruct the worker how to write markers, but they must not fabricate `Verdict: GO`. `Auto-Continue` remains `false` in generated terminal bodies by default; a real final worker flips markers only after verification.

## 6. Minimal implementation slices

After this design is reviewed GO, create this implementation graph on #hermes-main:

`m18->m19.channel_templates`

Suggested graph: legacy `impl -> review -> fanin` for #hermes-main, because it is implementing the template engine itself.

### Slice S1 — preset registry and validation

Files likely touched:

- new `src/agentflow_hermes/roadmap_templates.py`
- new `tests/test_roadmap_templates.py`
- extend `tests/test_roadmap_apply.py`

Work:

- define the three presets and role constants;
- implement `resolve_template(transition)` and role lookup;
- enforce exactly one review and one terminal step;
- prove unknown preset and mismatched explicit `slice_template` fail closed.

Acceptance:

- legacy `impl/review/fanin` still resolves;
- `research-loop` returns five ordered steps and terminal `brief`;
- `shaman-loop` returns five ordered steps and terminal `fanin`.

### Slice S2 — YAML/config evolution

Files likely touched:

- `src/agentflow_hermes/roadmap.py`
- `src/agentflow_hermes/roadmap_config.py`
- `tests/test_roadmap_autopromoter.py`
- `tests/test_loop_cli.py`

Work:

- add `template_preset` and `goal_anchor` to `RoadmapTransition`;
- load them from JSON/minimal YAML;
- include them in registry hash and receipts where appropriate;
- keep existing config tests passing unchanged.

Acceptance:

- old configs load with empty fields;
- new configs load and produce the preset-derived `slice_template`;
- mismatch errors prevent apply.

### Slice S3 — body generation and role-based profile

Files likely touched:

- `src/agentflow_hermes/roadmap.py`
- `tests/test_roadmap_apply.py`
- `tests/test_roadmap_autopromoter_watchdog.py`

Work:

- replace `kind == "fanin"` marker injection with terminal-role marker injection;
- add goal anchor/body sections to every candidate body;
- add review and terminal marker instructions;
- replace `_apply_task_profile(kind, ...)` string classification with role-based assignment.

Acceptance:

- research graph creates five tasks with assignees: work steps to `impl_assignee`, review to `review_assignee`, brief to `ack_trigger_agent`;
- only terminal gets `ack_trigger_agent`;
- review and terminal bodies include exact markers;
- no raw source summary copied into generated bodies.

### Slice S4 — scaffold UX for channel presets

Files likely touched:

- `src/agentflow_hermes/roadmap_register.py`
- `src/agentflow_hermes/cli.py`
- `tests/test_roadmap_register.py`

Work:

- add optional `roadmap init-config --template-preset {impl-review-fanin,research-loop,shaman-loop}`;
- add optional `--goal-anchor`;
- render preset sequence and validate by reloading config;
- keep default output exactly compatible with current generic scaffold.

Acceptance:

- current `init-config` tests continue passing;
- `--template-preset research-loop` emits `scout/evidence/scorecard/review/brief`;
- `--template-preset shaman-loop` emits `design/impl/browser_e2e/review/fanin`;
- invalid preset returns JSON error and no file clobber.

### Slice S5 — canary and docs

Files likely touched:

- `agentflow-roadmap.yaml`
- possibly `/home/duckran/.hermes/agentflow/roadmaps/research-roadmap.yaml` and `/home/duckran/.hermes/agentflow/roadmaps/shaman-roadmap.yaml` during operator-approved canary
- docs update if implementation changes contract details

Work:

- add #hermes-main transition `m18->m19.channel_templates`;
- canary request-only generation on `warroom-os` first;
- apply on `warroom-os` only after request-only output is inspected;
- repeat on `oracle-lab`;
- keep #hermes-main legacy preset as the control lane.

## 7. Canary plan using existing watchdog/promote path

No new scanner is needed. Use the active registry and existing `roadmap watch|promote` command path.

1. Request-only dry run on #research config:

```bash
agentflow-hermes roadmap watch \
  --config /home/duckran/.hermes/agentflow/roadmaps/research-roadmap.yaml \
  --once \
  --receipts-file /tmp/m19-research-canary-receipts.json
```

Expected:

- no `--apply`, so `applied: false` and no create calls;
- proposed kinds are `scout/evidence/scorecard/review/brief`;
- generated bodies include `#research` goal anchor;
- review and brief bodies include exact continuation marker instructions.

2. Apply canary on #research only after request-only inspection:

```bash
agentflow-hermes roadmap watch \
  --config /home/duckran/.hermes/agentflow/roadmaps/research-roadmap.yaml \
  --once --apply \
  --receipts-file /tmp/m19-research-canary-receipts.json
```

Expected:

- exactly five tasks created on board `warroom-os`;
- duplicate re-run with same receipts returns existing ids and creates zero new tasks;
- no cross-board fetch/create; all CLI calls use config board.

3. Repeat request-only then apply on #shaman / `oracle-lab`.

4. Monitor cron 8edf0c802844 using the existing watchdog registry. The registry already lists `agentflow-hermes-main`, `warroom-os`, and `oracle-lab`; it should not need a new Discord scanner.

5. Rollback is config-only: remove the new transition from `allowed_transitions` or set `enabled: false` / `kill_switch: true`. The double apply gate remains unchanged.

Cap note: both new presets are five tasks. Current `RoadmapPromotionPolicy.max_apply_tasks_per_graph` default is 5 and `apply_roadmap_promotion()` rejects only when `len(plan.candidates) > max_apply_tasks_per_graph`, so five-step graphs pass without raising the cap.

## 8. Final ACK schema standardization

All channel terminal bodies and final reports should converge on:

```text
Verdict: GO|BLOCK|NEED_MORE
Slice: <slice id/name>
Evidence: <links/artifacts/source refs or n/a>
Commit: <sha or none>
Tests: <commands and results or n/a>
Runtime smoke: <command/result or n/a>
Next: <operator/worker/user action>
Roadmap-Transition: <transition id>
Next-Slice: <next slice id>
Review-Edge: verified|missing
ACK-Edge: verified|missing
Parent-GO: verified|missing
Auto-Continue: false|true
Origin/return_to: <origin>
Return-To: <return_to>
Subscription-Status: verified|unverified
Policy-Resolution-Ref: <symbolic policy/config ref>
```

Channel-specific interpretation:

- #research `Evidence` should be source citations and scorecard artifacts; `Runtime smoke` can be `n/a` unless a script/tool was run.
- #shaman `Evidence` should include artifact paths/screenshots; `Runtime smoke` should include browser/e2e command/result when available.
- #hermes-main keeps existing commit/tests/runtime smoke expectations.

## 9. Code modules and tests likely touched

Modules:

- `src/agentflow_hermes/roadmap.py` — transition fields, role-based body/profile generation, terminal marker injection.
- `src/agentflow_hermes/roadmap_config.py` — YAML/JSON loading of `template_preset` and `goal_anchor`, preset-derived sequence validation.
- `src/agentflow_hermes/roadmap_register.py` — `init-config` template/anchor flags and render output.
- `src/agentflow_hermes/roadmap_cli.py` — likely no major change, but tests should prove existing promote/watch path carries bodies and roles correctly.
- `src/agentflow_hermes/cli.py` — only if adding CLI flags to `init-config` requires parser wiring.
- new `src/agentflow_hermes/roadmap_templates.py` — curated preset registry and validation.

Tests:

- new `tests/test_roadmap_templates.py`
- extend `tests/test_roadmap_apply.py`
- extend `tests/test_roadmap_autopromoter.py`
- extend `tests/test_roadmap_autopromoter_watchdog.py`
- extend `tests/test_roadmap_register.py`
- optionally extend `tests/test_loop_cli.py` only if CLI payload shape changes.

Minimum test cases:

- legacy config unchanged and still creates `impl/review/fanin`;
- research preset creates five correct kinds, assignee roles, and body markers;
- shaman preset creates five correct kinds and browser_e2e body objective;
- terminal marker injection works for `brief`, not just `fanin`;
- unknown preset/mismatched sequence fails closed;
- max task cap permits exactly five tasks and rejects six;
- request-only watch still creates no tasks;
- duplicate apply via receipts creates zero new tasks.

## 10. Risks and fail-closed constraints

- Config mismatch must be a load error, not a fallback to generic `impl/review/fanin`.
- Unknown preset must fail closed; do not infer channel from board name and silently select a template.
- Generated bodies must use symbolic `policy_refs`; do not inline route values or Claude model provider strings.
- `Auto-Continue: true` must never be pre-filled by template generation.
- Do not add a Discord reader/scanner. The only trigger remains board GO events found by `roadmap watch|promote`.
- Do not add live sends, restarts, gateway calls, or monetary actions.
- Review/fan-in marker text must be exact enough that downstream workers do not have to remember field names manually.
- Existing `same_board_only` and origin/return-to gates must stay exact-match per channel config.

## 11. Proposed M19 implementation graph

After this design gets review GO, create/allow this #hermes-main transition:

```yaml
allowed_transitions:
  - m16->m17.impl_review_fanin
  - m18->m19.channel_templates

transitions:
  m18->m19.channel_templates:
    roadmap_id: agentflow-hermes.roadmap
    from_slice: m18
    to_slice: m19
    template_preset: impl-review-fanin
    slice_template:
      - impl
      - review
      - fanin
    goal_anchor: "#hermes-main: implement bounded AgentFlow autopromoter template rendering, tests, and canary; no generic policy platform."
    policy_refs:
      - design_opus
      - implementation_default
    max_chain_depth: 3
    version: template-v2
```

Implementation graph name: `m18->m19.channel_templates`.

Recommended implementation task title:

`M19 impl: channel-specific AgentFlow template presets [m18->m19.channel_templates]`
