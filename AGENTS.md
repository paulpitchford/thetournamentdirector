# Repository agent constraints

These rules are supplied for productivity; deterministic controller and CI
checks remain authoritative.

- Never read, mount, copy, summarize, or commit ignored vendor/recovered paths,
  including `downloads/`, `extracted/`, and ignored `analysis/` directories.
- Work only on the approved named task branch and within its `allowedPaths`.
- Do not modify `.github/`, `.agent-workflow/`, quality configuration, lockfiles,
  or policy documents unless the task is explicitly approved as R3.
- Never weaken, skip, delete, or suppress a required check or test.
- Never access host credentials, the GitHub credential, Docker/Podman sockets,
  SSH material, browser state, keyrings, or the parent project directory.
- Use injected time, IDs, and randomness in domain logic. Store money in integer
  minor units. Keep the domain engine free of React, browser, network, and
  persistence dependencies.
- A completion message is not acceptance. Commit local work and return factual,
  structured evidence; the trusted controller decides progression.
