import pytest

from atlas.core.models import MissionPhase
from atlas.core.registry import AgentRegistry
from atlas.sim.scenario import (
    ApprovalStep,
    MessageStep,
    MissionReportStep,
    PhaseStep,
    ReportStep,
    load_scenarios,
)

EXPECTED = {"real-estate-investment", "eos-quarterly-diagnosis"}


@pytest.fixture(scope="module")
def scenarios():
    return load_scenarios(AgentRegistry.load())


def test_expected_scenarios_exist(scenarios):
    assert EXPECTED <= set(scenarios)


@pytest.mark.parametrize("scenario_id", sorted(EXPECTED))
def test_scenario_shape(scenarios, scenario_id):
    sc = scenarios[scenario_id]
    steps = sc.steps
    assert any(isinstance(s, ApprovalStep) for s in steps)
    assert any(isinstance(s, MessageStep) and s.type == "REQUEST" for s in steps)
    assert any(isinstance(s, ReportStep) for s in steps)
    assert sum(isinstance(s, MissionReportStep) for s in steps) == 1
    last = steps[-1]
    assert isinstance(last, PhaseStep) and last.phase == MissionPhase.CLOSED
    total = sum(s.wait for s in steps)
    assert 60 <= total <= 130, f"{scenario_id}: total wait {total:.1f}s"


@pytest.mark.parametrize("scenario_id", sorted(EXPECTED))
def test_oracle_findings_are_tagged(scenarios, scenario_id):
    reports = [s for s in scenarios[scenario_id].steps if isinstance(s, ReportStep) and s.agent == "oracle"]
    assert reports
    for rpt in reports:
        assert rpt.findings
        for claim in rpt.findings:
            assert claim.kind is not None
