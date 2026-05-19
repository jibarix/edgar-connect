"""Regression tests for the interactive-CLI integer prompt.

Before the fix, ``int(input(...))`` was called unguarded in
``interactive_mode``; a non-numeric keystroke raised an uncaught
``ValueError`` and aborted the session. ``prompt_int`` must instead
re-ask and never propagate ``ValueError``.
"""
import builtins

import main


def _scripted_input(responses):
    it = iter(responses)
    return lambda _prompt="": next(it)


def test_empty_response_returns_default(monkeypatch):
    monkeypatch.setattr(builtins, "input", _scripted_input([""]))
    assert main.prompt_int("n? ", 5) == 5


def test_valid_integer_is_parsed(monkeypatch):
    monkeypatch.setattr(builtins, "input", _scripted_input(["12"]))
    assert main.prompt_int("n? ", 5) == 12


def test_whitespace_is_stripped(monkeypatch):
    monkeypatch.setattr(builtins, "input", _scripted_input(["  7  "]))
    assert main.prompt_int("n? ", 5) == 7


def test_bad_input_reprompts_instead_of_crashing(monkeypatch):
    # "abc" used to crash the whole CLI; now it should re-ask and
    # eventually accept the next valid value.
    monkeypatch.setattr(builtins, "input", _scripted_input(["abc", "9"]))
    assert main.prompt_int("n? ", 5) == 9
