# Security policy

## Scope and design limits

The Shadow Trading Suite is **paper-trading only**. It reads no credentials, has no real order
path and no wallet or fund-movement functions. Its only inbound endpoint is a read-only web UI
bound to 127.0.0.1. Details: [docs/security.md](docs/security.md).

**Plugins are not sandboxed.** A plugin runs in the same Python process with the same
privileges as the suite. Install only plugins you trust and have reviewed.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's **"Report a vulnerability"** (private
security advisory) on this repository. Do not open a public issue.

Please include affected versions, steps to reproduce and the impact. Never include real
credentials or personal data in a report.

## Supported versions

Only the latest preview (`0.1.x` pre-releases) receives fixes. There is no stable release yet.
