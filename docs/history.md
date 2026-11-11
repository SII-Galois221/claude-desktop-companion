# Local Code history

This feature manages **Claude Desktop’s local Code history**. It does not move web chats, switch authentication, or manage passwords. Sign in and out using the official Claude Desktop app.

The initial public release supports the reviewed local-history schema of **Claude Desktop 5.1231.2**. Other versions stop before backup/import; do not add a version number to the registry merely to suppress the check. A maintainer must review the changed schema and behavior first.

## Register an account once

1. Install the official Claude app at `/Applications/Claude.app`. The initial release uses this exact installation path for version checks, quitting and reopening the app; other locations are not supported.
2. Sign in through the official app. Open a **local Code** session so that Claude creates its account and organization directories.
3. From the repository directory, run:

   ```bash
   printf '%s\n' '{"alias":"personal"}' | python3 backend/history_service.py register
   ```

4. Open **备份与导入** (Backup & Import) in Companion and refresh the status.

Use a different memorable alias after signing into another account, for example `work`. Aliases have 1–32 characters, begin with a lowercase letter, and contain lowercase letters, digits, `_` or `-`. Registration cannot replace an existing account mapping or rename an existing account.

If an account has more than one local organization directory, registration requires an explicit `org` field containing the intended existing directory name. Find that name under `~/Library/Application Support/Claude/claude-code-sessions/<current-account-uuid>/`. The command will not create a Claude organization directory or pick an ambiguous one automatically.

Registration extracts `lastKnownAccountUuid` from Claude’s `config.json` and lists the existing Code directory names. It stores only the alias, account UUID, organization UUID, and supported-version list. The surrounding `config.json` can contain sensitive data: it is never copied or logged. This tool does not access Keychain or save/change passwords, tokens, or cookies.

## Back up, sign in, import

1. Click **备份当前账号** (Back Up Current Account). The app saves a verified local checkpoint of the current account’s indexes, referenced transcript bodies and companion files.
2. Wait for backup completion. Sign out and sign in to the desired account in official Claude Desktop. Register that account once if needed.
3. Select a saved backup in Companion and preview the changes.
4. Confirm **导入到当前账号** (Import into Current Account). Claude must fully quit before import; the confirmation explains that background tasks may be interrupted.

Only saved checkpoints can be imported. The live source account directory is not required. Import into the same account is also supported for restoring missing history.

The target is backed up before writing. New indexes are appended, never substituted for existing ones. Missing transcript bodies are restored without overwriting existing files. A newer destination body is retained only when it extends the saved body as a strict byte prefix; divergent bodies stop the import. Account changes, changed files, incompatible schemas, stale previews, invalid paths and concurrent operations also stop it.

New entries use Manual permission mode and an unconfirmed resume state. Existing entries retain their original settings and may resume original tasks when Claude reopens. This is not an official import API: schema checks reduce risk but cannot guarantee compatibility with future Claude releases.

## Local storage and privacy

All Companion history data stays under:

```text
~/Library/Application Support/Claude-Desktop-Companion/history/
  accounts.json       # Local account/organization identifiers
  backups/            # Saved checkpoints, including conversation contents
  previews/           # Prepared plans
  transactions/       # Import receipts and staged data
```

Directories created by the history service use mode `700`; private files use mode `600`. Backups are **not encrypted by this app**. They include conversation text and may include secrets that were present in those conversations. Keep this entire directory private; do not commit it or attach it to GitHub issues. Copies made by OS backup or cloud-sync software follow that software’s settings.

Across all registered accounts, only the two newest verified complete checkpoints are kept in total. Older checkpoints are removed only after a successful backup or completed import. A failed operation does not rotate existing backups.

Backups contain metadata and local transcript files, not your project source tree, login credentials, or scheduled-task configuration. A prior target snapshot with missing transcript bodies is retained for inspection but is not offered as a complete import source.

The history service makes no network requests. Network monitoring, if explicitly configured, is a separate feature.

## CLI reference

The public interface is `backend/history_service.py`; `history_sync_engine.py` is an internal library.

```bash
python3 backend/history_service.py status
printf '%s\n' '{}' | python3 backend/history_service.py backup
printf '%s\n' '{"backup_id":"personal/<checkpoint-name>","target":"work"}' | python3 backend/history_service.py preview
```

Use the exact `backup_id` reported by the backup/status command. The preview returns a `preview_id`; after quitting Claude, pass both IDs and the target to `sync`. The graphical app handles these confirmations and state checks, so it is the recommended import interface. Do not share status, preview or checkpoint JSON without reviewing it for identifiers, paths, titles and conversation content.

If an import stops partway through, preserve the reported transaction directory, inspect its receipt locally, then make a fresh preview. Do not repeatedly replay an old plan or manually delete existing indexes to force a retry.

## Testing

```bash
python3 -B -m unittest discover -s backend -p test_history_service.py
```

Tests use temporary synthetic accounts, messages and paths; they do not modify installed Claude data or run real account imports.
