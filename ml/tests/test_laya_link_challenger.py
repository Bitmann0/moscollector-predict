from scripts.eval_laya_link_challenger import QUESTION, render_state


def test_laya_state_uses_only_observable_features():
    row = {"ch": 991234567, "obj": "private-site-name", "y": 1,
           "risk": 0.99999, "n_events": 2, "n_active_days_w7": 6,
           "gap_vs_own_rhythm": 3.5}
    state = render_state(row)
    assert "Reports today: 2" in state
    assert "past 7 days: 6" in state
    assert "3.5" in state
    assert "991234567" not in state
    assert "private-site-name" not in state
    assert "0.99999" not in state
    assert "y" not in QUESTION["unusual_gap"]
