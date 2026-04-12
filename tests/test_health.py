# SPDX-License-Identifier: AGPL-3.0-or-later
from nexus.health import HealthResult, format_health_for_cli


def test_health_result_fields():
    r = HealthResult(label="test", ok=True, detail="fine")
    assert r.label == "test"
    assert r.ok is True
    assert r.detail == "fine"
    assert r.fix_suggestions == []
    assert r.fatal is False


def test_health_result_with_fix_suggestions():
    r = HealthResult(
        label="missing key",
        ok=False,
        detail="not set",
        fix_suggestions=["nx config set key <value>", "https://example.com"],
        fatal=True,
    )
    assert r.fatal is True
    assert len(r.fix_suggestions) == 2


def test_format_check_ok():
    results = [HealthResult(label="Python ≥ 3.12", ok=True, detail="3.12.11")]
    output, failed = format_health_for_cli(results, local_mode=False)
    assert "✓ Python ≥ 3.12: 3.12.11" in output
    assert failed is False


def test_format_check_fail():
    results = [HealthResult(label="Python ≥ 3.12", ok=False, detail="3.11.0 — 3.12+ required")]
    output, failed = format_health_for_cli(results, local_mode=False)
    assert "✗ Python ≥ 3.12: 3.11.0 — 3.12+ required" in output
    # Non-fatal by default
    assert failed is False


def test_format_fatal_fail():
    results = [
        HealthResult(label="Python ≥ 3.12", ok=False, detail="3.11.0", fatal=True),
    ]
    output, failed = format_health_for_cli(results, local_mode=False)
    assert failed is True


def test_format_fix_suggestions():
    results = [
        HealthResult(
            label="ripgrep",
            ok=False,
            detail="not found",
            fix_suggestions=["brew install ripgrep", "apt install ripgrep"],
            fatal=True,
        ),
    ]
    output, failed = format_health_for_cli(results, local_mode=False)
    assert "    Fix: brew install ripgrep" in output
    assert "         apt install ripgrep" in output
    assert failed is True


def test_format_header():
    results = [HealthResult(label="test", ok=True)]
    output, _ = format_health_for_cli(results, local_mode=False)
    assert output.startswith("Nexus health check:\n")


def test_format_footer_cloud_mode():
    results = [HealthResult(label="key", ok=False, fatal=True)]
    output, _ = format_health_for_cli(results, local_mode=False)
    assert "nx config init" in output


def test_format_footer_local_mode():
    results = [HealthResult(label="path", ok=False, fatal=True)]
    output, _ = format_health_for_cli(results, local_mode=True)
    assert "Run 'nx doctor' again" in output


def test_format_no_footer_when_passing():
    results = [HealthResult(label="all good", ok=True)]
    output, failed = format_health_for_cli(results, local_mode=False)
    assert "nx config init" not in output
    assert failed is False


def test_format_no_detail():
    results = [HealthResult(label="test", ok=True)]
    output, _ = format_health_for_cli(results, local_mode=False)
    # No colon after label when detail is empty
    assert "  ✓ test\n" in output or output.endswith("  ✓ test")
