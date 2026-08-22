from horizon.pipeline.gate import CredibilityGate, GateStrategy, build_gate


def test_source_above_the_floor_passes():
    decision = CredibilityGate(0.2).evaluate(title="t", body="b", credibility_weight=0.5)
    assert decision.passed is True


def test_source_below_the_floor_is_dropped_as_lowcred():
    decision = CredibilityGate(0.2).evaluate(title="t", body="b", credibility_weight=0.1)
    assert decision.passed is False
    assert decision.status == "dropped_lowcred"
    assert "0.10" in decision.reason


def test_the_floor_itself_passes():
    """Spec drops weight < 0.2, so exactly 0.2 is admitted."""
    assert CredibilityGate(0.2).evaluate(title="t", body="b", credibility_weight=0.2).passed


def test_the_default_gate_satisfies_the_strategy_protocol():
    gate = build_gate()
    assert isinstance(gate, GateStrategy)
    assert gate.name == "credibility"
