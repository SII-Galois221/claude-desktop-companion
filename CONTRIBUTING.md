# Contributing

Small, focused issues and pull requests are welcome. Describe the user-visible problem and expected behavior before proposing a large feature. Read [SECURITY.md](SECURITY.md) and [PRIVACY.md](PRIVACY.md) before submitting diagnostics.

## Development

Use macOS 13 or newer, Xcode Command Line Tools, and Python 3.11 or newer. Python backend tests use the standard library; no package installation is needed.

```sh
python3 -m unittest discover -s backend -p 'test_*.py'
bash build-app.sh
```

The build compiles the native menu app and runs its model checks. Build artifacts are local and excluded from Git. A built app can access the real user's local data when opened; use a separate test macOS account for interactive testing. Do not start a development monitor against a live setup as part of an automated test.

For a Python interpreter outside the common Homebrew locations, launch the built executable with an explicit absolute path:

```sh
CLAUDE_COMPANION_PYTHON=/absolute/path/to/python3 "build/Claude Desktop Companion.app/Contents/MacOS/ClaudeDesktopCompanion"
```

This override must point to Python 3.11+ and applies to that launch. No interpreter or machine-specific path is bundled.

## Code and tests

- Keep the two history actions straightforward: back up the active account; import a selected saved backup into the active account.
- Keep account login in the official Claude Desktop app. Do not add password, cookie, token, or Keychain export/switching features.
- Preserve version/schema checks, stopped-app checks, preview revalidation, target backups, and no-clobber file writes. Do not bypass them to make a test pass.
- Test filesystem changes inside temporary directories with fabricated UUIDs and transcripts. Mock process termination and OS service operations.
- Use reserved documentation IP addresses and placeholder domains in examples. Never commit real account aliases/IDs, home directories, conversation text, proxy endpoints, or subscription URLs.
- Make status uncertainty visible. Stale, partial, unconfigured, or unverified observations must not appear healthy.
- Keep new dependencies minimal and document any outbound network requests.

## Pull requests

Explain the problem, final behavior, relevant tests, and any compatibility or privacy implications. Update the README or detailed docs when user-facing behavior changes. State whether testing was synthetic or performed against a supported Desktop version; do not imply real imports were tested when they were not.

For a new Claude Desktop version, review the local index/transcript schema, imported-session behavior and permissions, then add meaningful synthetic regression cases. An allowlist-only update is insufficient.

Contributions are provided under the repository's [MIT license](LICENSE). Do not copy vendor application code, private backups, or third-party code without compatible licensing and attribution.
