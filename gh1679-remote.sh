#!/usr/bin/env bash
set -u
scripts/build-gate-jar.sh || { echo "JAR_BUILD_FAILED"; exit 90; }
echo "=== STEP readers ==="
python3 scripts/watched_pytest.py --stall 300 --status /tmp/gh1679r3-readers.json -- uv run pytest tests/identity tests/auras tests/hooks tests/mcp tests/daemon/test_supervisor_stop_grace.py tests/test_group_cmd.py tests/test_health_service_checks.py tests/test_hooks.py tests/test_identity_cmd.py tests/test_llm_backend_cli_identity.py tests/test_llm_openai_backend.py tests/test_operator_delegated_identity.py tests/test_repo_identity_stability.py tests/test_subscriptions_identity.py tests/test_subscriptions.py tests/test_vc_runtime_provision.py -p no:cacheprovider
echo "READERS_EXIT=$?"
echo "=== STEP lint ==="
python3 scripts/watched_pytest.py --stall 300 --status /tmp/gh1679r3-lint.json -- uv run pytest -m lint -n 4 -p no:cacheprovider
echo "LINT_EXIT=$?"
