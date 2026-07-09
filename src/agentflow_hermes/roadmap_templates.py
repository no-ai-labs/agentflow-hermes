"""Closed roadmap template preset registry for channel-specific AgentFlow graphs."""

from __future__ import annotations

from dataclasses import dataclass

ROLE_WORK = "work"
ROLE_REVIEW = "review"
ROLE_TERMINAL = "terminal"
VALID_ROLES = {ROLE_WORK, ROLE_REVIEW, ROLE_TERMINAL}
LEGACY_PRESET = "impl-review-fanin"


@dataclass(frozen=True)
class RoadmapTemplateStep:
    kind: str
    role: str
    objective: str


@dataclass(frozen=True)
class RoadmapTemplatePreset:
    name: str
    lane: str
    slice_template: tuple[str, ...]
    steps: tuple[RoadmapTemplateStep, ...]
    goal_anchor: str

    def step_for(self, kind: str) -> RoadmapTemplateStep:
        for step in self.steps:
            if step.kind == kind:
                return step
        raise ValueError(f"template preset {self.name} has no step: {kind}")


@dataclass(frozen=True)
class ResolvedRoadmapTemplate:
    name: str
    lane: str
    goal_anchor: str
    steps: tuple[RoadmapTemplateStep, ...]

    @property
    def slice_template(self) -> tuple[str, ...]:
        return tuple(step.kind for step in self.steps)

    def step_for(self, kind: str) -> RoadmapTemplateStep:
        for step in self.steps:
            if step.kind == kind:
                return step
        raise ValueError(f"resolved template {self.name} has no step: {kind}")


_PRESETS: dict[str, RoadmapTemplatePreset] = {
    LEGACY_PRESET: RoadmapTemplatePreset(
        name=LEGACY_PRESET,
        lane="#hermes-main / agentflow-hermes",
        slice_template=("impl", "review", "fanin"),
        goal_anchor="#hermes-main / agentflow-hermes exists to ship reviewed, ACKed AgentFlow-Hermes implementation slices without channel-policy sprawl.",
        steps=(
            RoadmapTemplateStep("impl", ROLE_WORK, "Implement the bounded slice and produce concrete commit/test evidence."),
            RoadmapTemplateStep("review", ROLE_REVIEW, "Review the implementation fail-closed and emit GO/BLOCK/NEED_MORE with continuation markers when GO."),
            RoadmapTemplateStep("fanin", ROLE_TERMINAL, "Fan in implementation and review evidence, verify the ACK edge, and report the final roadmap continuation state."),
        ),
    ),
    "research-loop": RoadmapTemplatePreset(
        name="research-loop",
        lane="#research / warroom-os",
        slice_template=("scout", "evidence", "scorecard", "review", "brief"),
        goal_anchor="#research / warroom-os exists to turn signals into sourced, scored, operator-useful research briefs; do not drift into generic implementation tasks or unverified speculation.",
        steps=(
            RoadmapTemplateStep("scout", ROLE_WORK, "Find the candidate topic/source/event, define the research question, and bound the scope."),
            RoadmapTemplateStep("evidence", ROLE_WORK, "Collect primary-source links, citations, numbers, contradictions, and uncertainty notes."),
            RoadmapTemplateStep("scorecard", ROLE_WORK, "Rank confidence, impact, actionability, and red flags from the gathered evidence."),
            RoadmapTemplateStep("review", ROLE_REVIEW, "Review source quality, evidence sufficiency, scorecard logic, and lane fit; fail closed on unsupported claims."),
            RoadmapTemplateStep("brief", ROLE_TERMINAL, "Produce the concise final research brief with citations, scorecard outcome, ACK evidence, and continuation markers."),
        ),
    ),
    "shaman-loop": RoadmapTemplatePreset(
        name="shaman-loop",
        lane="#shaman / oracle-lab",
        slice_template=("design", "impl", "browser_e2e", "review", "fanin"),
        goal_anchor="#shaman / oracle-lab exists to build accumulated oracle/wiki/KB artifacts with browser-verifiable UX; do not drift into generic guardrail work or unsupported esoteric claims.",
        steps=(
            RoadmapTemplateStep("design", ROLE_WORK, "Design the bounded artifact UX/IA/content contract for oracle-lab/wiki/KB experiences."),
            RoadmapTemplateStep("impl", ROLE_WORK, "Implement the bounded artifact, config, or template slice with concrete evidence."),
            RoadmapTemplateStep("browser_e2e", ROLE_WORK, "Run browser/user-flow smoke for generated or UI artifacts and capture runtime evidence."),
            RoadmapTemplateStep("review", ROLE_REVIEW, "Review drift, safety, raw-envelope leaks, browser/e2e evidence, and final-report readiness."),
            RoadmapTemplateStep("fanin", ROLE_TERMINAL, "Fan in design, implementation, browser smoke, and review evidence into the final ACK report."),
        ),
    ),
}


def preset_names() -> tuple[str, ...]:
    return tuple(sorted(_PRESETS))


def get_template_preset(name: str) -> RoadmapTemplatePreset:
    preset_name = str(name or LEGACY_PRESET)
    try:
        return _PRESETS[preset_name]
    except KeyError as exc:
        raise ValueError(f"unknown template_preset: {preset_name}") from exc


def resolve_template(
    *,
    template_preset: str = "",
    slice_template: tuple[str, ...] = (),
    goal_anchor: str = "",
) -> ResolvedRoadmapTemplate:
    """Resolve a transition's sequence, roles, and anchor from the closed registry.

    Empty template_preset preserves legacy compatibility: keep the transition's
    explicit sequence and infer roles by convention (review kind = review, final
    step = terminal, everything else = work). A named preset is fail-closed: the
    explicit sequence, when supplied, must exactly match the preset sequence.
    """

    preset = get_template_preset(template_preset or LEGACY_PRESET)
    explicit = tuple(str(x) for x in (slice_template or ()))
    if template_preset:
        if explicit and explicit != preset.slice_template:
            raise ValueError("slice_template does not match template_preset")
        steps = preset.steps
    else:
        sequence = explicit or preset.slice_template
        steps = tuple(_legacy_step(kind, index, len(sequence), preset) for index, kind in enumerate(sequence))
    resolved = ResolvedRoadmapTemplate(
        name=preset.name if template_preset or not explicit or explicit == preset.slice_template else "legacy-inferred",
        lane=preset.lane,
        goal_anchor=str(goal_anchor or preset.goal_anchor),
        steps=steps,
    )
    _validate_resolved(resolved)
    return resolved


def _legacy_step(kind: str, index: int, total: int, preset: RoadmapTemplatePreset) -> RoadmapTemplateStep:
    if kind in preset.slice_template:
        preset_step = preset.step_for(kind)
        if preset.slice_template == ("impl", "review", "fanin"):
            return preset_step
    role = ROLE_REVIEW if kind == ROLE_REVIEW else ROLE_TERMINAL if index == total - 1 else ROLE_WORK
    objective = (
        f"Review the {kind} step and emit a fail-closed verdict."
        if role == ROLE_REVIEW
        else f"Complete terminal fan-in/ACK for the {kind} step."
        if role == ROLE_TERMINAL
        else f"Complete the {kind} work step with concrete evidence."
    )
    return RoadmapTemplateStep(kind=kind, role=role, objective=objective)


def _validate_resolved(template: ResolvedRoadmapTemplate) -> None:
    if not template.steps:
        raise ValueError("template requires at least one step")
    kinds = [step.kind for step in template.steps]
    if len(set(kinds)) != len(kinds):
        raise ValueError("template step kinds must be unique")
    for step in template.steps:
        if not step.kind.replace("_", "").replace("-", "").isalnum():
            raise ValueError("template step kind must be identifier-safe")
        if step.role not in VALID_ROLES:
            raise ValueError("template step role must be work/review/terminal")
    if sum(1 for step in template.steps if step.role == ROLE_REVIEW) != 1:
        raise ValueError("template requires exactly one review step")
    if sum(1 for step in template.steps if step.role == ROLE_TERMINAL) != 1:
        raise ValueError("template requires exactly one terminal step")
    if template.steps[-1].role != ROLE_TERMINAL:
        raise ValueError("terminal step must be final")


for _preset in _PRESETS.values():
    _validate_resolved(ResolvedRoadmapTemplate(_preset.name, _preset.lane, _preset.goal_anchor, _preset.steps))
