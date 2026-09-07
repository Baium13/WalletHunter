# Wallet Hunter — controlled baseline

This repository records the existing application under `WalletHunterV05_final/`.
Phase 0 adds reproducibility, documentation and offline verification only. It does
not repair trading defects, change policies, deploy code or rewrite runtime state.

Start with [Phase 0 status and safe commands](docs/PHASE0.md).

- [Exact dependencies and installation](docs/DEPENDENCIES.md)
- [Environment contract and actual defaults](docs/ENVIRONMENT.md)
- [Existing entrypoints and modules](docs/ENTRYPOINTS.md)
- [Sensitive backup/recovery inventory](docs/RECOVERY.md)
- [Unresolved Phase 1 findings](docs/PHASE1_FINDINGS.md)
- [Machine-readable unit-test baseline](docs/baseline-tests.json)
- [Auxiliary browser-test status](docs/browser-baseline.json)

The original application README is retained byte-for-byte as historical source;
its older interface/deployment instructions are not the authoritative baseline.
Never run bot/web entrypoints or old deployment/recovery scripts as a test.
Never commit `.env`, keys, sessions, financial state or backup archives.

No remote publication or production deployment is part of this baseline.
