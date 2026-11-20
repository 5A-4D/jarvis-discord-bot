# Publication snapshot

This private repository was prepared on November 20, 2026 from an existing
personal Jarvis deployment. The initial publication commit is a sanitized source
snapshot; it does not import the original deployment's Git history.

## Included

- Application modules, including implemented features that were not yet committed
  in the original deployment workspace.
- Existing regression tests, with synthetic examples replacing deployment identifiers.
- Generated status artwork, source dependencies and operational references.
- A starter configuration with empty credential/account fields and optional public
  access and voice features disabled.

## Excluded

- Filled environment files and calendar/feed credentials.
- Discord conversations, voice archives, identities and personal checklists.
- Runtime databases, browser state, session history and generated request outputs.
- Codex/GitHub authentication, host SSH material and private backup images.
- The original operational Git history.

The running deployment is maintained in a separate workspace. Changes in this
publication workspace are not automatically deployed to it.

## Preparation

Deployment account IDs were replaced with synthetic values. Host-specific paths
were converted to examples or derived from the current home/project location.
Service files are configuration templates rather than installed units.

The source and publication history are checked using Gitleaks with redacted
reporting, plus a local exclusion check against configured deployment secrets.
Private scan reports remain outside this repository. These checks provide
supporting evidence, not a guarantee that every possible sensitive value is detected.

## Validation

- 528 regression tests passed in the existing Python 3.13 runtime image, with
  network disabled, source mounted read-only, capabilities dropped, and no
  production home, data, credentials, Docker socket or systemd bus mounted.
- Python source parsing and shell syntax checks passed.
- The parameterized Compose document passed configuration validation with
  synthetic paths and an empty temporary environment file. A new Docker image
  was not built or deployed as part of publication preparation.
- Gitleaks directory scanning reported zero findings. A separate private check
  found no matches for configured deployment credentials or account IDs.
- The voice receive extension is pinned to the revision installed in the checked
  runtime; direct Python dependencies retain the tested version selections.
- GitHub Actions runs the regression suite on pushes and pull requests. Initial
  remote execution is verified separately after upload.

Some tests intentionally simulate failure and log warnings. The suite also emits
an existing SQLite ResourceWarning and a Discord dependency deprecation warning;
these did not fail validation. No live Discord messages or infrastructure changes
were performed by the publication test run.
