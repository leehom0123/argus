# Security Policy

## Supported Versions

Argus is pre-v0.1 and under active development. Only the latest commit on
the `main` branch receives security fixes. There are no backports to older
commits or tags at this stage.

| Version           | Supported          |
| ----------------- | ------------------ |
| `main` (latest)   | :white_check_mark: |
| Anything else     | :x:                |

## Reporting a Vulnerability

If you discover a security vulnerability, **please do not open a public
GitHub issue**. Instead, report it privately by email:

- **leehom0123@gmail.com**

Please include:

- A description of the issue and its potential impact
- Steps to reproduce, or a proof-of-concept where applicable
- Affected commit / branch / deployment surface
- Your name or handle, if you would like to be credited

You will receive an acknowledgement within **7 days**. We will keep you
informed throughout triage and remediation.

## Disclosure Window

We follow a **90-day coordinated disclosure** policy. We aim to ship a fix
and publish an advisory within 90 days of the initial report. If more time
is needed, we will agree on an extended timeline with the reporter.

## Out of Scope

The following are intentionally out of scope and will not be treated as
security issues:

- Rate-limiting, denial-of-service, or stress-testing of the public demo
  deployment
- Access to the public demo project (it is intentionally open and may
  contain sample data)
- Findings that require physical access, social engineering, or
  privileged local access to the host machine
- Self-hosted deployments that have disabled authentication or run with
  default development credentials

Thank you for helping keep Argus and its users safe.
