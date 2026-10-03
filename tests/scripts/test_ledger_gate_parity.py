"""nexus-hcdk3: ``--ledger-only`` and ``--client-precondition`` (the floor
and the deploy-order modes of ``check_engine_release_floor.py``, once two
scripts) must return the same ledger verdict for the same wire-contract ledger.

They share one parser and one classifier of the ``[additive]`` token
(``check_wire_contract_pairing.classify_unshipped``). This suite pins the
agreement itself, fixture by fixture, and once against
the REAL checked-in ledger — the case that was live-red on 2026-09-01 with
no test on either side able to see it (tests/scripts/conftest.py isolates
every test onto an empty ledger unless marked ``real_ledger``).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

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

_FIXTURES: dict[str, tuple[str, int]] = {
    "empty": ("(none)\n", 0),
    "tokenless": (_TOKENLESS, 1),
    "additive": (_ADDITIVE, 0),
    "not-additive": (_NOT_ADDITIVE, 1),
    "mixed": (_ADDITIVE + _NOT_ADDITIVE, 1),
    "both-tokens": (_BOTH_TOKENS, 1),
}


def _verdicts() -> tuple[int, int]:
    # The DATA EFFECT relay leg of --client-precondition is out of scope here.
    with patch.object(floor, "check_data_effect_relay", return_value=0):
        return (
            floor.main(["--ledger-only"]),
            floor.main(["--client-precondition", "engine-service-v9.9.9"]),
        )


@pytest.mark.parametrize("name", sorted(_FIXTURES))
def test_both_modes_agree_on_fixture(name: str, tmp_path: Path) -> None:
    body, expected = _FIXTURES[name]
    path = tmp_path / "wire-contract-pending.md"
    path.write_text(f"## Unshipped\n\n{body}\n## Shipped\n", encoding="utf-8")
    with patch.object(ledger_mod, "DEFAULT_LEDGER_PATH", path):
        ledger_rc, precond_rc = _verdicts()
    assert ledger_rc == precond_rc == expected, (name, ledger_rc, precond_rc)


def test_fixture_set_is_not_vacuous() -> None:
    """Both verdict values must occur, or a gate that returned a constant
    would pass every parity row."""
    assert {rc for _, rc in _FIXTURES.values()} == {0, 1}


@pytest.mark.real_ledger
def test_both_modes_agree_on_the_checked_in_ledger() -> None:
    """The exact case that was live on 2026-09-01: two [additive] entries,
    ledger-only exit 1, precondition exit 0."""
    real = ledger_mod.DEFAULT_LEDGER_PATH
    assert real.is_file() and real.name == "wire-contract-pending.md"
    ledger_rc, precond_rc = _verdicts()
    assert ledger_rc == precond_rc, (ledger_rc, precond_rc)
