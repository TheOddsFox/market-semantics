import json

import pytest

from market_semantics.cli import main
from market_semantics.contracts import SemanticsError


def test_invalid_cli_arguments_return_two(monkeypatch):
    monkeypatch.setattr("sys.argv", ["market-semantics", "compile"])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 2


@pytest.mark.parametrize("failure,exit_code", [("TIMEOUT", 124), ("INVALID_INPUT", 1)])
def test_processing_errors_have_json_stderr(monkeypatch, capsys, failure, exit_code):
    monkeypatch.setattr(
        "sys.argv", ["market-semantics", "compile", "--selection", "/s", "--output", "/o"]
    )

    def fail(*arguments):
        raise SemanticsError(failure, "controlled failure")

    monkeypatch.setattr("market_semantics.runtime.compile_release", fail)
    assert main() == exit_code
    result = capsys.readouterr()
    assert not result.out
    assert json.loads(result.err)["code"] == failure


def test_keyboard_interruption_returns_130(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv", ["market-semantics", "compile", "--selection", "/s", "--output", "/o"]
    )

    def interrupt(*arguments):
        raise KeyboardInterrupt

    monkeypatch.setattr("market_semantics.runtime.compile_release", interrupt)
    assert main() == 130
    assert json.loads(capsys.readouterr().err)["code"] == "INTERRUPTED"
