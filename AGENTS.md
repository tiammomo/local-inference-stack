# Local Inference Stack maintenance rules

- Treat `/home/tiammomo/projects/infra/local-inference-stack` as the only project root.
- Do not recreate or depend on the retired `/home/tiammomo/projects/infra/models` path.
- Keep model artifacts, runtime profiles, deployment manifests, operations scripts,
  acceptance tests, and project documentation inside this repository.
- Keep reusable API, routing, accounting, and Tool Use protocol behavior in the
  ModelPort repository; do not duplicate ModelPort source code here.
- Run commands from the project root. After runtime-only changes, run
  `./scripts/acceptance-suite.sh quick`. After protocol, reasoning, token-counting,
  or Tool Use changes, run `./scripts/acceptance-suite.sh standard`.
- Preserve the stable integration contract in
  `contracts/local-qwen-provider-v1.json` unless a coordinated migration updates
  both this project and ModelPort.
- Record production configuration or acceptance-baseline changes in `docs/`.
