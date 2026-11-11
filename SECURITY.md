# Security

This project handles local conversation history and can request that Claude Desktop exit after a detected network fault. It is experimental software, not an account-protection guarantee, a firewall, or an immediate network kill switch.

## Reporting a vulnerability

If the repository's **Security → Report a vulnerability** action is available, use [GitHub private vulnerability reporting](https://github.com/SII-Galois221/claude-desktop-companion/security/advisories/new).

If that action is unavailable, open an issue asking for a private reporting channel **without** disclosing exploit details or sensitive data. Do not post backups, credentials, proxy configuration, real account identifiers, or unredacted logs in a public issue. There is no guaranteed response time.

Include the Companion version/commit, macOS and Claude Desktop versions, the affected component, expected and actual behavior, and a minimal reproduction using fabricated accounts and transcripts.

## Security boundaries

- **Local trust:** file permissions and checksums help detect errors and unintended changes. They do not defend against malicious software running as the same macOS user or an administrator. Backups are not encrypted.
- **History import:** imports require Claude Desktop to be fully stopped, a current preview, and supported metadata. The target is backed up before writes. Existing indexes are not overwritten; conflicting transcript content stops the operation. A partial failure can still leave successfully added files, recorded in the transaction log.
- **Session permissions:** newly imported indexes use manual permissions and are marked unconfirmed for resumption. Existing entries retain their own settings. Reopening Claude can resume existing tasks according to Claude's behavior.
- **Version support:** local history formats are undocumented and can change. The program blocks unreviewed Desktop versions and unsupported fields. Adding a version number to an allowlist is not a compatibility review.
- **Network monitoring:** fault detection and application exit take time. Connection sampling can miss short-lived flows, some UDP activity, unrelated CLI processes, or processes it cannot attribute. A displayed route is an observation, not a guarantee that all traffic followed it.
- **Configuration:** use a trusted local HTTP proxy and local Mihomo controller. This app does not provision or audit your proxy provider, establish residential IP ownership, or enforce a no-fallback policy on your behalf.

The initial public release is source-built and ad-hoc signed. It is not an Apple-notarized release. Review build instructions and code before running it. Download this project's source only from its canonical repository.

## Maintainer expectations

Safety fixes should include a synthetic regression test. Changes to account detection, filesystem writes, proxy validation, or process termination require explicit review of their failure modes. Never reproduce a report against a contributor's real conversation store in CI.
