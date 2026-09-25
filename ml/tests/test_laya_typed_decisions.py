from scripts.eval_laya_typed_decisions import QUESTIONS, QUESTION_HASH, _choice


def test_typed_workflow_has_all_required_question_ids():
    assert set(QUESTIONS) == {"action", "needs_review", "outcome", "risk", "urgency"}
    assert QUESTIONS["needs_review"]["type"] == "noul"
    assert QUESTIONS["action"]["type"] == "choice"
    assert QUESTIONS["risk"]["type"] == "score"
    assert len(QUESTION_HASH) == 16


def test_choice_parser_accepts_laya_typed_answer():
    assert _choice({"choice": "manual_review"}) == "manual_review"
    assert _choice({"value": "monitor"}) == "monitor"
    assert _choice({}) is None
