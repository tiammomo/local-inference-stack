# Local Inference Stack agent contract

This repository is designed to be operated by an Agent after a fresh clone.
Treat the directory containing this file as `PROJECT_ROOT`; never assume a user,
home directory, parent directory, GPU, or adjacent ModelPort checkout.

## First-run workflow

1. Read `README.md`, `docs/GETTING_STARTED.md`, `docs/HARDWARE_GUIDE.md`, and
   `catalog/models.json` before changing host state.
2. Run `./stack plan --json`. The current implementation may use internal
   compatibility adapters, but those are not public entry points. This is the only default first-run
   command: it is read-only and reports detected hardware,
   prerequisites, recommendation status, size, and a typed action plan when
   deployment admission succeeds.
3. Show the user the selected model, `evidenceStatus`, `hostAcceptanceStatus`,
   download size, immutable source revision, license metadata, SHA256 policy,
   context, current free VRAM, and caveats. Do not download, select, start, stop,
   or install services until the user explicitly approves those state changes.
4. After approval, prefer `./stack deploy --model CATALOG_ID --yes`; use only
   catalog-backed commands with `--yes`; never invent a
   filename, URL, hash, hardware threshold, or unreviewed model entry.
5. A successful `./stack deploy` already includes quick acceptance; do not run
   it a second time. Use the documented internal quick command only for a
   standalone recheck or a contributor verification task. A provisional or
   estimated recommendation is not promoted by quick acceptance alone.
6. Record genuinely reusable host validation as a deployment manifest under
   `deployments/`; do not relabel estimates as validated.
7. Treat `validated-on-this-host` as valid only when `hostAcceptanceEvidence`
   points to a fresh, secure, matching standalone schema v4 record or a schema
   v5 record backed by its exact completed upgrade receipt. Hardware-profile
   similarity alone is not host acceptance, and a busy running GPU may still
   make `readyToDeploy` false.

## Upgrade and rollback workflow

- Inspect `./stack upgrade --model CATALOG_ID` or `./stack rollback` first. The
  commands are read-only without `--yes`; upgrade shows the admitted source,
  target, scope, selected qualification tier, and caveats, while rollback also
  shows its verified pointer and typed plan. Present those facts before asking
  for approval.
- Only `./stack upgrade --model CATALOG_ID [--qualification {quick,full}] --yes`
  and `./stack rollback --yes`
  may execute the typed maintenance-window lifecycle. Do not call internal
  replacement flags or runtime adapters directly.
- Rollback spec v1 is deliberately limited to
  `same-controller-same-catalog-anchor-v1`: the same trusted WSL2 x86_64 host,
  an exact clean controller material set, an exact current Catalog rollback
  entry, the `latency` Profile, and already-local artifacts and image identity.
- Rollback never downloads, pulls, checks out Git, or reconstructs an anchor
  from mutable state. A controller, Catalog, host, evidence, artifact, Compose,
  image, or pointer mismatch is a hard stop; restore those prerequisites under
  a separately reviewed procedure rather than bypassing the check.
- Upgrade and rollback run `quick --no-record` inside the transaction as a
  service gate. That check creates no reusable host evidence, does not qualify
  a model, and does not promote a Catalog entry.
- Upgrade defaults to `--qualification quick`, preserving the v1 rollout plan.
  `--qualification full` is a v2 rollout plan with a distinct `target-full`
  action before rollback publication. It requires an absolute, current-user,
  clean ModelPort checkout whose live loopback build has the same Git identity.
  The passed schema v5 evidence is useful only after the exact completed
  transaction receipt resolves; a failed, restored, pending, substituted, or
  manually copied record is not qualification authority.
- Do not run transaction-bound full acceptance through
  `scripts/acceptance-suite.sh` directly. The public upgrade command supplies
  the pending action authority and deterministic evidence identity. Standalone
  schema v4 acceptance remains a local compatibility path, not a substitute
  for a missing v5 rollout receipt.
- The current executable Catalog contains only one provisional entry,
  so no real upgrade pair or rollback anchor is eligible. This is expected to
  fail closed; never manufacture an entry, pointer, evidence file, or local
  artifact to make the command proceed.

## Safety and boundaries

- Model artifacts, generated profiles, cache, logs, and secrets are local and
  ignored by Git. Never stage them or print credential files.
- Downloads require explicit `--yes`, use HTTPS, retain resumable `.part` files,
  and are promoted only after exact byte-size and SHA256 verification.
- The catalog pins third-party GGUF artifacts; review the upstream model and
  artifact licenses before use. A hash proves identity, not trustworthiness.
- Automatic new deployment currently requires the exact Tier-1 WSL2 x86_64 /
  RTX 5070 Ti profile. Native Linux and other NVIDIA hosts remain read-only
  until qualified. For CPU, Apple Silicon, AMD, unusual multi-GPU, or shared production GPUs, stop after
  the plan and design a reviewed profile rather than forcing this Compose stack.
- For a new deployment, `readyToDeploy=false` or a missing typed `actionPlan`
  is a hard stop. Upgrade and rollback have their own typed source, replacement,
  and anchor admission; any failed admission is equally final. Do not work
  around free-VRAM, multi-GPU, platform, trust, or prerequisite checks.
- Keep services loopback-bound. Do not expose port `18080` or the operations
  dashboard without adding authentication and a separate security review.

## Repository ownership

- This repository owns model selection, artifact integrity, llama.cpp runtime,
  GPU/KV/context profiles, acceptance, benchmarks, deployment evidence, and
  aggregate operations data.
- ModelPort owns reusable authentication, routing, accounting, Anthropic/OpenAI
  edge protocols, and Tool Use adaptation. Applications own actual tool
  execution, approval, sandboxing, and business logic.
- The direct OpenAI-compatible llama.cpp API is the standalone first-run path.
  ModelPort is optional; `standard` and `full` acceptance require an explicitly
  supplied compatible checkout via `MODELPORT_PROJECT_DIR`.
- Preserve `contracts/local-qwen-provider-v1.json` unless both repositories are
  migrated together. Do not duplicate ModelPort source here.

## Change verification

- Runtime/catalog/docs changes: unit tests, catalog plans at representative VRAM
  boundaries, Compose config validation, and `acceptance-suite.sh quick`.
- Reasoning, token, Tool Use, or ModelPort contract changes:
  `MODELPORT_PROJECT_DIR=/path/to/ModelPort ./scripts/acceptance-suite.sh standard`.
- Production configuration or acceptance baseline changes must be documented.
