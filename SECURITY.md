# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security vulnerability. Use [GitHub's private vulnerability reporting](https://github.com/FlyLikeAPenguin/nitpick/security/advisories/new) when available, or contact the maintainer through the GitHub profile.

Include:

- the affected version or commit
- a description of the issue and its impact
- reproducible steps or a minimal proof of concept
- any suggested mitigation

Please redact API keys, tokens, production log contents, and private source code. We will acknowledge reports as soon as practical and coordinate disclosure after a fix or mitigation is available.

## Scope notes

Nitpick handles error logs and can provide configured code paths to Claude CLI. Treat those inputs as sensitive, review provider permissions, and run the dashboard only on trusted local interfaces. The dashboard is bound to `127.0.0.1` by default but is not an authentication boundary.
