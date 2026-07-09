from __future__ import annotations

import pytest

from agentflow_hermes.roadmap_config import load_repo_roadmap_config
from agentflow_hermes.roadmap_templates import ROLE_REVIEW, ROLE_TERMINAL, ROLE_WORK, resolve_template


def test_legacy_template_resolves_impl_review_fanin_roles():
    template = resolve_template(slice_template=("impl", "review", "fanin"))

    assert template.slice_template == ("impl", "review", "fanin")
    assert [step.role for step in template.steps] == [ROLE_WORK, ROLE_REVIEW, ROLE_TERMINAL]


def test_research_template_resolves_closed_sequence_and_roles():
    template = resolve_template(template_preset="research-loop")

    assert template.slice_template == ("scout", "evidence", "scorecard", "review", "brief")
    assert [step.role for step in template.steps] == [ROLE_WORK, ROLE_WORK, ROLE_WORK, ROLE_REVIEW, ROLE_TERMINAL]
    assert "sourced, scored" in template.goal_anchor


def test_shaman_template_resolves_browser_e2e_as_work_role():
    template = resolve_template(template_preset="shaman-loop")

    assert template.slice_template == ("design", "impl", "browser_e2e", "review", "fanin")
    assert template.step_for("browser_e2e").role == ROLE_WORK
    assert template.step_for("review").role == ROLE_REVIEW
    assert template.step_for("fanin").role == ROLE_TERMINAL


def test_unknown_preset_and_sequence_mismatch_fail_closed():
    with pytest.raises(ValueError, match="unknown template_preset"):
        resolve_template(template_preset="freeform-loop")
    with pytest.raises(ValueError, match="does not match"):
        resolve_template(template_preset="research-loop", slice_template=("impl", "review", "fanin"))


def test_repo_config_derives_preset_sequence_when_slice_template_omitted(tmp_path):
    path = tmp_path / "roadmap.yaml"
    path.write_text(
        "\n".join([
            "enabled: true",
            "board: warroom-os",
            "transitions:",
            "  research.default.scout_evidence_scorecard_review_brief:",
            "    roadmap_id: research.roadmap",
            "    from_slice: research-current",
            "    to_slice: research-next",
            "    template_preset: research-loop",
            "    goal_anchor: \"custom research anchor\"",
            "    policy_refs:",
            "      - design_opus",
            "      - implementation_default",
        ])
        + "\n",
        encoding="utf-8",
    )

    config = load_repo_roadmap_config(path)
    transition = config.transitions["research.default.scout_evidence_scorecard_review_brief"]
    assert transition.slice_template == ("scout", "evidence", "scorecard", "review", "brief")
    assert transition.template_preset == "research-loop"
    assert transition.goal_anchor == "custom research anchor"
