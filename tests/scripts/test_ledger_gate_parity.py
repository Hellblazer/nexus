"""nexus-hcdk3: the floor gate and the precondition gate must return the
same verdict for the same wire-contract ledger.

They share one parser and, since the fix, one classifier of the
``[additive]`` token (``check_wire_contract_pairing.classify_unshipped``).
This suite pins the agreement itself, fixture by fixture, and once against
the REAL checked-in ledger — the case that was live-red on 2026-09-01 with
no test on either side able to see it (tests/scripts/conftest.py isolates
every test onto an empty ledger unless marked ``real_ledger``).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import check_client_release_precondition as precond
import check_engine_release_floor as floor
import check_wire_contract_pairing as ledger_mod

_TOKENLESS = (
    "- `deadbeefdeadbeefdeadbeefdeadbeefdeadbeef` -- bead nexus-fake -- "
    "engine tag `engine-service-v9.9.9` -- test fixture\n"
)
_ADDITIVE = (
    "- `cafebabecafebabecafebabecafebabecafebabe` -- bead nexus-addv -- "
    "engine tag `engine-service-v9.9.9` -- [additive] old client + new engine safe\n"
)
_NOT_ADDITIVE = (
    "- `feedfacefeedfacefeedfacefeedfacefeedface` -- bead nexus-notad -- "
    "engine tag `engine-service-v9.9.9` -- [not-additive] deploy must be armed\n"
)
_BOTH_TOKENS = (
    "- `beadbeadbeadbeadbeadbeadbeadbeadbeadbead` -- bead nexus-both -- "
    "engine tag `engine-service-v9.9.9` -- [additive] but also [not-additive]\n"
)

_FIXTURES: dict[str, tuple[str, list[str] | None, int]] = {
    "empty": ("(none)\n", None, 0),
    "tokenless": (_TOKENLESS, None, 1),
    "tokenless-acked": (_TOKENLESS, ["nexus-fake"], 0),
    "tokenless-wrong-ack": (_TOKENLESS, ["nexus-other"], 1),
    "additive": (_ADDITIVE, None, 0),
    "not-additive": (_NOT_ADDITIVE, None, 1),
    "mixed": (_ADDITIVE + _NOT_ADDITIVE, None, 1),
    "mixed-non-additive-acked": (_ADDITIVE + _NOT_ADDITIVE, ["nexus-notad"], 0),
    "both-tokens": (_BOTH_TOKENS, None, 1),
}


def _verdicts(path: Path, ack: list[str] | None) -> tuple[int, int]:
    with patch.object(ledger_mod, "DEFAULT_LEDGER_PATH", path):
        floor_rc = floor.check_client_lag_ledger(ack)
        precond_rc, _vacuous = precond.check_wire_contract_ledger(ack)
    return floor_rc, precond_rc


@pytest.mark.parametrize("name", sorted(_FIXTURES))
def test_both_gates_agree_on_fixture(name: str, tmp_path: Path) -> None:
    body, ack, expected = _FIXTURES[name]
    path = tmp_path / "wire-contract-pending.md"
    path.write_text(f"## Unshipped\n\n{body}\n## Shipped\n", encoding="utf-8")
    floor_rc, precond_rc = _verdicts(path, ack)
    assert floor_rc == precond_rc == expected, (name, floor_rc, precond_rc)


def test_fixture_set_is_not_vacuous() -> None:
    """Both verdict values must occur, or a gate that returned a constant
    would pass every parity row."""
    assert {rc for _, _, rc in _FIXTURES.values()} == {0, 1}


@pytest.mark.real_ledger
def test_both_gates_agree_on_the_checked_in_ledger() -> None:
    """The exact case that was live on 2026-09-01: two [additive] entries,
    floor exit 1, precondition exit 0."""
    real = ledger_mod.DEFAULT_LEDGER_PATH
    assert real.is_file() and real.name == "wire-contract-pending.md"
    floor_rc = floor.check_client_lag_ledger()
    precond_rc, _ = precond.check_wire_contract_ledger()
    assert floor_rc == precond_rc, (floor_rc, precond_rc)
