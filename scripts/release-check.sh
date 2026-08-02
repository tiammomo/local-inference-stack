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
git diff --cached --check
while IFS= read -r document; do
  python3 -m json.tool "$document" >/dev/null
done < <(git ls-files -co --exclude-standard -- '*.json')
python3 -m compileall -q src scripts tests
python3 -m unittest discover -s tests -p 'test_*.py' -v
bash -n scripts/*.sh scripts/lib/*.sh
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck scripts/*.sh scripts/lib/*.sh
elif [[ "${REQUIRE_SHELLCHECK:-false}" == "true" ]]; then
  printf 'shellcheck is required but not installed.\n' >&2
  exit 1
else
  printf 'NOTE: shellcheck is not installed; shell lint skipped.\n'
fi
if command -v node >/dev/null 2>&1; then
  expected_node_major="$(tr -d '[:space:]' < .nvmrc)"
  actual_node_major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || true)"
  if [[ "$actual_node_major" != "$expected_node_major" ]]; then
    if [[ "${REQUIRE_NODE:-false}" == "true" ]]; then
      printf 'Node.js major %s is required by .nvmrc; found %s.\n' \
        "$expected_node_major" "$(node --version 2>/dev/null || printf unknown)" >&2
      exit 1
    fi
    printf 'NOTE: .nvmrc requires Node.js %s; using %s only for the optional syntax check.\n' \
      "$expected_node_major" "$(node --version 2>/dev/null || printf unknown)"
  fi
  node --check dashboard/app.js
elif [[ "${REQUIRE_NODE:-false}" == "true" ]]; then
  printf 'node is required but not installed.\n' >&2
  exit 1
else
  printf 'NOTE: node is not installed; dashboard syntax check skipped.\n'
fi
python3 scripts/check-doc-links.py
python3 scripts/check-doc-commands.py
./stack config check --json >/dev/null
./stack reference --check --json >/dev/null
python3 scripts/install-user-services.py --check
python3 -c 'from scripts.runtime_identity import rendered_compose; rendered_compose("latency")' \
  >/dev/null
python3 -c 'from scripts.runtime_identity import rendered_compose; rendered_compose("throughput")' \
  >/dev/null
python3 -c 'from scripts.runtime_identity import rendered_compose; import json; print(json.dumps(rendered_compose("latency")))' \
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
python3 scripts/verify-manifest.py
python3 scripts/model-manager.py plan --json >/dev/null
./stack plan --json >/dev/null

ignored_tracked="$(git ls-files -ci --exclude-standard || true)"
if [[ -n "$ignored_tracked" ]]; then
  printf 'Tracked files unexpectedly match .gitignore:\n%s\n' "$ignored_tracked" >&2
  exit 1
fi

if command -v rg >/dev/null 2>&1 && rg --version >/dev/null 2>&1; then
  forbidden="$(git ls-files | rg '(^|/)(\.env|.*\.gguf|.*\.part|operations\.secrets\.env|deployment\.local\.env)$' || true)"
else
  forbidden="$(git ls-files | grep -E '(^|/)(\.env|.*\.gguf|.*\.part|operations\.secrets\.env|deployment\.local\.env)$' || true)"
fi
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
if command -v rg >/dev/null 2>&1 && rg --version >/dev/null 2>&1; then
  absolute_home_files="$(rg -l "${home_prefix}[^/ ]+" README.md AGENTS.md docs scripts deploy catalog compose.yaml || true)"
else
  absolute_home_files="$(grep -RIlE "${home_prefix}[^/ ]+" README.md AGENTS.md docs scripts deploy catalog compose.yaml || true)"
fi
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
