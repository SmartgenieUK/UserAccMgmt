"""R7-F2 (CR-208, BRIEF-016): PLATFORM_OPERATOR_USER_IDS is read from the environment as a JSON array."""
from __future__ import annotations

from app.core.config import Settings

OPERATOR = "3f2c9a6e-1b4d-4e8a-9c7f-2d5b8e1a6c40"


def test_operator_list_parses_a_json_array_from_the_environment(monkeypatch):
    monkeypatch.setenv("PLATFORM_OPERATOR_USER_IDS", f'["{OPERATOR}"]')
    assert Settings(_env_file=None).PLATFORM_OPERATOR_USER_IDS == [OPERATOR]


def test_operator_list_unset_is_empty(monkeypatch):
    monkeypatch.delenv("PLATFORM_OPERATOR_USER_IDS", raising=False)
    assert Settings(_env_file=None).PLATFORM_OPERATOR_USER_IDS == []
