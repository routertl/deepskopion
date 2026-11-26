# GitHub Synchronization Architecture

This document describes the repository synchronization model between the canonical Bitbucket repository and the public GitHub repository.

---

## 1. Overview

Deepskopion maintains two repository homes:

1. **Bitbucket (Canonical / Hardware Bench Integration)**
   - Authoritative source of truth for ongoing engineering, internal design reviews, and automated hardware qualification on physical FPGA test benches.
   - Hosts full internal test suites, hardware captures, and board infrastructure automation.

2. **GitHub (Public Distribution & Community Collaboration)**
   - Public open-source mirror and distribution point for the FPGA datalogger design, capture daemons, protocol specifications, and user documentation.
   - Receives community contributions, issue reports, bug fixes, and third-party board ports.

---

## 2. Orphan Tree Architecture

GitHub is populated from a **fresh `git init` over a scrubbed working tree**, rather than a history rewrite of Bitbucket:

- **No historical exposure**: Historical commits in an internal repository can retain transient build outputs, preliminary internal design notes, or credentials. Rewriting history (`git filter-repo`) carries risks of retaining historical artifacts in git packfiles.
- **Scrubbed working tree**: Every export generates an orphan working tree containing only released files, strictly excluding internal documentation, automation configs, and commercial models via the export drop list.
- **Reproducible provenance**: Every sync commit created in the GitHub repository is stamped with clear provenance lines referencing the authoritative Bitbucket source commit hash and timestamp.

---

## 3. Bidirectional Sync Direction

Synchronization operates as a continuous, re-runnable lifecycle across two explicit directions:

```
                  ┌──────────────────────────────┐
                  │          Bitbucket           │
                  │   (Canonical Development     │
                  │    & Hardware Validation)    │
                  └──────────────┬───────────────┘
                                 │
                   Outbound Sync │ tools/export_github.py
                   (Automated)   │ Hygiene gate exit condition
                                 ▼
                  ┌──────────────────────────────┐
                  │            GitHub            │
                  │     (Public Distribution     │
                  │    & Community PR Hub)       │
                  └──────────────┬───────────────┘
                                 │
                    Inbound Sync │ PR review & fetch
                    (Manual/PR)  │ HW bench qualification
                                 ▼
                  ┌──────────────────────────────┐
                  │          Bitbucket           │
                  │    (Reconciled & Tested)     │
                  └──────────────────────────────┘
```

### Outbound Sync (Bitbucket → GitHub)

1. Qualified changes on the Bitbucket repository are ready for public release.
2. The export script (`tools/export_github.py`) runs from a separate checkout at the source commit, targeting a clone of the public repository:
   ```bash
   git worktree add --detach /path/to/export-worktree <source-sha>
   cd /path/to/export-worktree
   python3 tools/export_github.py --dest /path/to/github/repo --sha <source-sha>
   ```
   The hygiene gate that judges an export is the one the source commit ships. From a checkout at any other commit, or one with tracked changes, the script refuses before it writes anything to `--dest`.
3. The script executes the following stages:
   - **Tracked file export**: Extracts tracked files from the specified Bitbucket commit hash.
   - **Drop list exclusion**: Filters out internal documentation, internal agent workflows, and private modules.
   - **Host path regeneration**: Sanitizes any host-specific paths in generation reports and verifies that large binary overlays adhere to clean pointer specifications.
   - **Hygiene gate validation**: Runs `tools/public_artifact_hygiene.py --export-gate`. If any secret, credential, internal reference, or developer path is detected, the script **refuses to proceed and aborts before any git commit is created**.
   - **Provenance stamping**: Produces a single commit on the destination branch containing `Source-Commit` and `Source-Date` headers.
4. The stamped commit is pushed to GitHub.

### Inbound Sync (GitHub → Bitbucket)

1. External contributors submit issues and pull requests on GitHub following the guidelines in `CONTRIBUTING.md`.
2. PRs are reviewed by maintainers for alignment with protocol contracts and architecture.
3. Maintainers fetch the contributor branch into an isolated Bitbucket worktree:
   ```bash
   git fetch github pull/<PR_ID>/head:pr-<PR_ID>
   git checkout pr-<PR_ID>
   ```
4. The contributor's gateware and software changes undergo physical hardware validation on real FPGA development boards.
5. Once physical verification passes and regression suites are green, the PR is merged into Bitbucket.
6. The next outbound sync cycle automatically reflects the merged contribution on GitHub with full provenance.
