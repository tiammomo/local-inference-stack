#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WITH_RUNTIME="false"
GITLEAKS_IMAGE="zricethezav/gitleaks@sha256:ebfeb6fd4f2c37fa371d3731ebfa662fdf80f93cd37d3b4771bb82263edff8d0"
if [[ "${1:-}" == "--with-runtime" ]]; then
  WITH_RUNTIME="true"
elif [[ $# -gt 0 ]]; then
  printf 'Usage: %s [--with-runtime]\n' "$0" >&2
  exit 2
fi

cd "$ROOT_DIR"
git diff --check
python3 -m json.tool catalog/models.json >/dev/null
python3 -m unittest discover -s tests -p 'test_*.py' -v
bash -n scripts/*.sh scripts/lib/*.sh
docker compose --env-file profiles/latency.env config --quiet
docker compose --env-file profiles/latency.env config --format json \
  | python3 -c '
import json
import sys

config = json.load(sys.stdin)
port = config["services"]["qwen35"]["ports"][0]
assert port["host_ip"] == "127.0.0.1", port
assert str(port["published"]) == "18080", port
'
scripts/candidate-runtime.sh config \
  | python3 -c '
import json
import sys

config = json.load(sys.stdin)
service = config["services"]["qwen35"]
port = service["ports"][0]
assert config["name"] == "qwen35-candidate", config["name"]
assert service["container_name"] == "qwen35-9b-candidate", service["container_name"]
assert port["host_ip"] == "127.0.0.1", port
assert str(port["published"]) == "18081", port
'
python3 scripts/model-manager.py plan --json >/dev/null

forbidden="$(git ls-files | rg '(^|/)(\.env|.*\.gguf|.*\.part|operations\.secrets\.env|deployment\.local\.env)$' || true)"
if [[ -n "$forbidden" ]]; then
  printf 'Forbidden tracked local artifacts:\n%s\n' "$forbidden" >&2
  exit 1
fi

secret_files="$(git grep -IlE '(BEGIN (RSA|OPENSSH|EC) PRIVATE KEY|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})' -- . || true)"
if [[ -n "$secret_files" ]]; then
  printf 'Potential secret patterns in tracked files:\n%s\n' "$secret_files" >&2
  exit 1
fi

home_prefix="/""home/"
absolute_home_files="$(rg -l "${home_prefix}[^/ ]+" README.md AGENTS.md docs scripts deploy catalog compose.yaml || true)"
if [[ -n "$absolute_home_files" ]]; then
  printf 'Non-portable absolute home paths found in public project files:\n%s\n' "$absolute_home_files" >&2
  exit 1
fi

if command -v gitleaks >/dev/null 2>&1; then
  gitleaks git --redact --no-banner
elif docker image inspect "$GITLEAKS_IMAGE" >/dev/null 2>&1; then
  docker run --rm -v "$ROOT_DIR:/repo:ro" -w /repo \
    "$GITLEAKS_IMAGE" git --redact --no-banner --no-color
elif [[ "${REQUIRE_GITLEAKS:-false}" == "true" ]]; then
  printf 'gitleaks is required but neither the binary nor pinned image is available.\n' >&2
  exit 1
else
  printf 'NOTE: gitleaks is not installed; history scan skipped.\n'
fi

if [[ "$WITH_RUNTIME" == "true" ]]; then
  scripts/acceptance-suite.sh quick
fi

printf 'Release checks passed. Review git diff and third-party licenses before push.\n'
