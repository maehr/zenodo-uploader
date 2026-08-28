# Security Policy

## Reporting a Vulnerability

To report a security issue, please email **moritz.maehr@gmail.com** with a description
of the issue, the steps to reproduce it, affected versions, and, if known, any
mitigations. Please do **not** open a public GitHub issue for security reports.

This project follows a 90-day disclosure timeline. You can expect an initial
acknowledgement within a few days, and updates as the report is investigated,
accepted, or declined.

## Handling tokens

`zenodo-uploader` uses personal access tokens (`ZENODO_TOKEN` and
`ZENODO_SANDBOX_TOKEN`) that grant write access to your Zenodo account. To keep them
safe:

- **Never** paste a token into an issue, pull request, log, or screenshot. Redact it
  before sharing any output.
- Keep tokens in a local `.env` file (git-ignored) — see [`.env.example`](.env.example).
- Scope tokens to the minimum needed: `deposit:write` and `deposit:actions`.
- Revoke a token immediately at
  <https://zenodo.org/account/settings/applications/> if it is ever exposed.
