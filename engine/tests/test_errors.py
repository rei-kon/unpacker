"""Классификация ошибок §5.4 — по структурному контракту, не по regex вслепую.

Из ResultMessage.subtype/api_error_status и типа исключения строим Outcome, по которому
AgentCore решает: продолжить, повторить, поднять health-алерт или честно вернуть ошибку.
"""

from claude_agent_sdk import ProcessError

from engine.core.errors import Outcome, classify_exception, classify_result


class FakeResult:
    def __init__(self, subtype="success", is_error=False, api_error_status=None, errors=None):
        self.subtype = subtype
        self.is_error = is_error
        self.api_error_status = api_error_status
        self.errors = errors


def test_success_is_ok():
    assert classify_result(FakeResult(subtype="success")).kind == "ok"


def test_max_turns():
    o = classify_result(FakeResult(subtype="error_max_turns", is_error=True))
    assert o.kind == "max_turns"


def test_exec_error():
    o = classify_result(FakeResult(subtype="error_during_execution", is_error=True))
    assert o.kind == "exec_error"


def test_auth_error_by_api_status():
    o = classify_result(FakeResult(subtype="success", api_error_status=401))
    assert o.kind == "auth_error"  # 401 бьёт даже при subtype=success


def test_other_error():
    o = classify_result(FakeResult(subtype="error_weird", is_error=True, errors=["boom"]))
    assert o.kind == "other_error"


def test_classify_exception_auth_from_stderr():
    exc = ProcessError(
        "command failed", exit_code=1, stderr="OAuth token expired: 401 Unauthorized"
    )
    assert classify_exception(exc).kind == "auth_error"


def test_classify_exception_auth_from_message():
    assert classify_exception(RuntimeError("authentication_failed")).kind == "auth_error"


def test_classify_exception_generic():
    assert classify_exception(RuntimeError("random glitch")).kind == "other_error"


def test_classify_exception_no_false_auth_on_bare_401():
    """Голое «401» в постороннем контексте не должно давать ложный auth_error (ложный алерт)."""
    for text in ("Error at line 401", "path /home/proj401/file", "retry count 4013"):
        assert classify_exception(RuntimeError(text)).kind == "other_error", text


def test_outcome_is_frozen():
    o = Outcome(kind="ok", detail="")
    assert o.kind == "ok"
