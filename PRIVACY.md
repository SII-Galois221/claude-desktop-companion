# Privacy

Claude Desktop Companion is an independent, local macOS utility. It has no project-operated service, telemetry, analytics, account system, or automatic crash reporting.

## Local history

The history component reads Claude Desktop's local Code session indexes and the transcript files they reference. It reads `lastKnownAccountUuid` from Claude Desktop's `config.json` to identify the active account. That file is parsed locally; it is not copied into a backup or sent anywhere. Registration also reads the local account/organization directory names. The component does not use the Keychain, log in, export cookies, or store login credentials.

Backups contain **conversation text and metadata**. They can contain source snippets, local paths, tool outputs, attachments stored in referenced transcript directories, and any secrets you previously included in a conversation. They are not redacted or encrypted by this application. File permissions restrict new backup files to the current macOS user; this is not protection from another program running as that user.

The app does not upload backups. Importing makes local history available to the currently signed-in Claude Desktop account. If you later open or continue an imported session, Claude Desktop may send that conversation context to Anthropic as part of normal operation. Its behavior and privacy terms apply independently of this tool.

## Network monitoring

Network monitoring is optional and requires an explicit local configuration. The app uses local process/socket information and Mihomo's connection table to correlate observable Claude Desktop connections with the configured proxy node. It does not inspect packet contents or collect process command-line arguments or environment variables. The local controller response can include other applications' connection metadata; only matching Claude connections are included in this app's audit output.

When monitoring is active and Claude is running, these third-party requests are used:

| Destination | Purpose | Typical interval |
| --- | --- | --- |
| `https://www.gstatic.com/generate_204` | Connectivity check through the configured HTTP proxy | 10 seconds |
| `https://www.apple.com/library/test/success.html` | Fallback when the primary connectivity check fails | On failure |
| `https://www.cloudflare.com/cdn-cgi/trace` | Compare the proxy's observed public IP with the expected IP | About 60 seconds, while connection sampling is active |

These services see ordinary request metadata, including the address that reaches them. The probe requests do not include Claude account identifiers or conversation data. The program explicitly selects the configured local proxy for probes. Your proxy operator and these services have their own privacy policies. A successful probe is not proof that every Claude connection uses the same route.

Pausing monitoring stops the external probes. Quitting the menu app stops its monitor. Existing proxy settings are not removed by either action.

## Files stored on this Mac

Application-owned data is stored under:

```text
~/Library/Application Support/Claude-Desktop-Companion/
```

It includes the local account registry, selected network settings and expected public IP, backups, import previews and transaction records, runtime control files, and connection audit information. Connection audits can include destination hosts/IPs, proxy chain names and the observed egress IP. History metadata can include session titles and paths. Treat this whole directory as private.

The public repository and build script contain only source, documentation, and synthetic test data. Local account registries, network configuration, logs and backups are not bundled into the app.

## Retention and sharing

Backups and transaction records are retained until you remove them; there is no automatic cloud synchronization or remote deletion mechanism. Quit the app before managing its data directory. Removing that directory deletes its backups and configuration, but does not delete Claude Desktop's original conversations or change proxy settings managed outside this app.

Before filing an issue, inspect and redact diagnostic output. Do not attach real backups, account registries, `config.json`, proxy configuration, session titles, IP addresses, or screenshots containing private information. Use synthetic examples where possible. See [SECURITY.md](SECURITY.md) for sensitive reports.
