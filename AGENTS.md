# Local Inference Stack agent contract

This repository is designed to be operated by an Agent after a fresh clone.
Treat the directory containing this file as `PROJECT_ROOT`; never assume a user,
home directory, parent directory, GPU, or adjacent ModelPort checkout.

## First-run workflow

1. Read `README.md`, `docs/GETTING_STARTED.md`, `docs/HARDWARE_GUIDE.md`, and
   `catalog/models.json` before changing host state.
2. Run `./scripts/model-manager.py plan --json`. This is the only default
   first-run command: it is read-only and reports detected hardware,
   prerequisites, recommendation status, size, and next commands.
3. Show the user the selected model, `evidenceStatus`, download size, source,
   SHA256 policy, context, and caveats. Do not download, select, start, stop, or
   install services until the user explicitly approves those state changes.
4. After approval, use only catalog-backed commands with `--yes`; never invent a
   filename, URL, hash, hardware threshold, or unreviewed model entry.
5. Run `./scripts/acceptance-suite.sh quick` after deployment. A recommendation
   marked `estimated` remains a candidate until it passes acceptance on that host.
6. Record genuinely reusable host validation as a deployment manifest under
   `deployments/`; do not relabel estimates as validated.

## Safety and boundaries

- Model artifacts, generated profiles, cache, logs, and secrets are local and
  ignored by Git. Never stage them or print credential files.
- Downloads require explicit `--yes`, use HTTPS, retain resumable `.part` files,
  and are promoted only after exact byte-size and SHA256 verification.
- The catalog pins third-party GGUF artifacts; review the upstream model and
  artifact licenses before use. A hash proves identity, not trustworthiness.
- Automation currently supports Linux/WSL x86_64 NVIDIA CUDA hosts. For CPU,
  Apple Silicon, AMD, unusual multi-GPU, or shared production GPUs, stop after
  the plan and design a reviewed profile rather than forcing this Compose stack.
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
