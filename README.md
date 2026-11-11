# Claude Desktop Companion

[简体中文](README.zh-CN.md) · [History guide](docs/history.md) · [Network guide](docs/network.md) · [Privacy](PRIVACY.md)

![macOS 13+](https://img.shields.io/badge/macOS-13%2B-black)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)
[![MIT license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Checks](https://github.com/SII-Galois221/claude-desktop-companion/actions/workflows/checks.yml/badge.svg)](https://github.com/SII-Galois221/claude-desktop-companion/actions/workflows/checks.yml)

A small macOS menu bar app for **backing up local Claude Code history, importing saved history into the currently signed-in Claude Desktop account, and optionally monitoring an existing proxy connection**.

**Early source release.** The interface is currently Chinese. History support is restricted to the reviewed **Claude Desktop 5.1231.2** schema. This independent project is not affiliated with Anthropic; it cannot prevent account suspension or guarantee network isolation.

## What it does

| Feature | Behavior |
| --- | --- |
| Back Up Current Account | Saves local Code indexes, referenced transcripts and companion files into a verified local checkpoint. |
| Import into Current Account | Previews a saved backup, backs up the target, then adds missing history after Claude exits. |
| Optional network monitor | Displays sampled connections and exit-IP checks; requests Claude termination on configured local failures or repeated reachability failures. |

You sign in through **official Claude Desktop**. Companion does not store passwords, switch tokens, transfer web chats, or upload backups. [Read exactly what it accesses.](PRIVACY.md)

## Build and open

Requirements: macOS 13+, Xcode Command Line Tools, Python 3.11+ and official Claude Desktop. Python uses only its standard library. Install Python with your preferred trusted installer; the GUI searches both Homebrew locations. Custom interpreters are described in [the development guide](CONTRIBUTING.md).

```bash
xcode-select --install       # Only if the command line tools are missing
git clone https://github.com/SII-Galois221/claude-desktop-companion.git
cd claude-desktop-companion
bash build-app.sh
open "build/Claude Desktop Companion.app"
```

The build runs Swift model checks and ad-hoc signs the local app. It is **not notarized** and no prebuilt download is offered in this initial release. Keep the app's name unchanged. It can also be copied to `~/Applications/` after building.

Opening the app with no network configuration leaves monitoring off. History features can be used independently.

## Back up and import in four steps

1. Sign in to Claude Desktop and open a **local Code** session. Register that account once from the repository:

   ```bash
   printf '%s\n' '{"alias":"personal"}' | python3 backend/history_service.py register
   ```

2. Open **备份与导入** (Backup & Import) from the menu bar or press **⌘1**. Click **备份当前账号** (Back Up Current Account) and wait for completion.
3. Sign in to the target account in official Claude Desktop. Register it once with another alias, such as `work`, and refresh Companion.
4. Select the saved backup, preview it, then click **导入到当前账号** (Import into Current Account). The app explains the changes and asks to quit Claude before writing.

```mermaid
flowchart LR
    A[Current local Code history] --> B[Verified local backup]
    B --> C[Preview against current account]
    C --> D[Back up target]
    D --> E[Append missing history]
```

Existing indexes are preserved. A newer transcript is retained when it extends the saved bytes; divergent contents block the import. New entries use manual permissions and require resume confirmation; existing entries keep their settings. Quitting Claude may interrupt background tasks. Read the [history guide](docs/history.md) before the first import, including storage, version checks and recovery behavior.

## Optional proxy monitoring

Configure this only if you already have a working **dedicated loopback HTTP proxy and Mihomo Unix control socket**. Companion records your chosen trust baseline; it does not install a proxy, create a residential connection or configure Claude's routing.

Follow [network setup](docs/network.md) to register the listener, expected final node, expected public IP and configuration files. Opening Companion starts configured monitoring; pausing or quitting stops automatic termination. There is no independent login service.

**This is a polling monitor, not a firewall or TUN.** Unexpected sampled chains/IPs are warnings, not automatic termination triggers. Short-lived connections, UDP and independently launched CLI processes are not comprehensively covered. Probes generate small periodic network requests; the amount depends on your proxy and transport. A green state summarizes recent observations, not every packet.

## Local data

```text
~/Library/Application Support/Claude-Desktop-Companion/
├── history/       # Private account registry, backups, previews and receipts
├── network.json   # Optional local monitor baseline
└── runtime/       # Monitor state and logs
```

Backups are local and **not encrypted by the app**. Conversations can contain sensitive information. Never upload this directory, real transcripts, tokens or proxy configurations to an issue. The repository contains synthetic test fixtures only.

To uninstall, quit Companion and remove its app bundle. Local backups remain. Remove the data directory separately only when you intentionally want to discard it. Companion does not remove or change Claude's own proxy settings.

## Development

```bash
python3 -B -m unittest discover -s backend -p 'test_*.py'
python3 scripts/check_public_tree.py
bash build-app.sh
```

The backend tests use synthetic temporary data; CI does not sign in to Claude or import real histories. [Contributing](CONTRIBUTING.md) · [Security reporting](SECURITY.md) · [MIT license](LICENSE)

### Project layout

```text
Sources/     Swift/AppKit menu bar and backup/import windows
backend/     Standard-library Python history and network services + tests
docs/        Setup and operational details
scripts/     Public-tree hygiene checks
.github/     CI and issue templates
```

Documentation organization was informed by [Stats](https://github.com/exelban/stats), [Maccy](https://github.com/p0deje/Maccy) and [Clash Verge Rev](https://github.com/clash-verge-rev/clash-verge-rev). Their code, artwork and documentation are not bundled here.
