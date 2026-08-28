# The Tournament Director — research workspace

Static-analysis workspace for The Tournament Director, a Windows desktop application for operating live poker tournaments.

## Headline findings

- Current public release: **3.7.2**; the vendor update manifest dates it **2020-10-09**.
- Supported by the vendor on Windows 10 and 11; the installer is an NSIS executable.
- The application is an **Electron 5.0.10 / Chromium 73** desktop app written mainly in prototype-oriented JavaScript, HTML, and CSS.
- Its central model holds the tournament clock, levels, players, transactions, prizes, seating, display layout, events, rules, and history.
- The operator uses a 17-tab Settings window; players see a separate full-screen Game window with tournament, rankings, seating, movement, and schedule pages.
- Tournament and template files are plaintext JavaScript-like object serializations, not JSON or a relational database.
- The release bundle contains 172 AES-CTR-obfuscated JavaScript libraries. `tools/decrypt-source.js` reproduces the application's own loader for local inspection.
- Important security debt exists: an unsupported Electron runtime, privileged renderer, no context isolation/CSP, and `eval`-based loading of tournament/template files.

No downloaded executable has been run. No licensing control has been modified or bypassed.

## Documents

- [Next-agent handoff](HANDOFF.md)
- [Product behaviour](docs/PRODUCT.md)
- [Technical architecture and findings](docs/TECHNICAL.md)
- [Clean-room reimplementation plan](docs/REIMPLEMENTATION.md)
- [Modern cross-platform MVP plan](docs/MODERN_APP_PLAN.md)
- [Agent orchestration research](docs/AGENT_ORCHESTRATION_RESEARCH.md)
- [Guarded agent delivery workflow](docs/AGENT_ORCHESTRATION_PLAN.md)
- [Engineering quality policy](docs/ENGINEERING_QUALITY_POLICY.md)
- [Policy-as-code enforcement](docs/POLICY_ENFORCEMENT.md)
- [Agent capacity and limits policy](docs/AGENT_CAPACITY_POLICY.md)
- [Agent-ready delivery backlog](docs/DELIVERY_BACKLOG.md)
- [Architecture decision records](docs/adr/README.md)
- [Artifacts, integrity, and sources](docs/ARTIFACTS.md)

## Workspace map

```text
downloads/                 Vendor installer
extracted/installer/       Installer contents
extracted/app/             Unpacked Electron app.asar
analysis/decrypted/        Locally recovered application libraries
analysis/signatures/       Extracted Authenticode certificate containers
research/raw/              Saved official pages, guide, manifest, file list
research/screenshots/      Official product screenshots
tools/decrypt-source.js    Reproducible source-recovery helper
docs/                      Product, technical, workflow, and delivery plans
```

## Reproduce the source extraction

From this directory:

```bash
node tools/decrypt-source.js
```

The recovered code is for local interoperability, security review, and behavioural study. The vendor license prohibits redistribution of the application, so keep `downloads/`, `extracted/`, and `analysis/decrypted/` private.
