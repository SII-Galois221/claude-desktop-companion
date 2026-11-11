# Optional network monitoring

Network monitoring is independent of local history backup/import. If you only want history tools, leave it unconfigured. The app does not start the monitor, probe external sites, or request Claude's exit without a valid local network manifest.

This is a **sampling monitor with delayed exit requests**, not a firewall, TUN implementation, or account-ban prevention tool. It does not install, select, repair, or purchase a proxy. You remain responsible for configuring and checking your existing network route.

## Requirements

- macOS 13 or later, Python 3.11 or later, and the built app named **Claude Desktop Companion.app**. Keep that name: owner-process checks use the bundle executable path.
- For monitoring, official Claude Desktop installed at **`/Applications/Claude.app`**. A Claude installation elsewhere is outside this monitor's process scope, even if history tools can find it.
- An existing dedicated **HTTP proxy listener on a numeric loopback address**, such as `http://127.0.0.1:17897`. Remote listeners, URL credentials, URL paths, and hostnames are rejected by setup.
- A local Mihomo-compatible controller exposed through an absolute **Unix socket path**. TCP controller endpoints and controller authentication secrets are not implemented in this release.
- A reviewed final outbound node name and expected public IP address. The chain must not fall back to `DIRECT` or another exit when that node fails.
- Existing configuration files whose contents you have reviewed and want to monitor for changes.

Configure Claude's fixed proxy through the mechanism supported by your installed Claude version before enabling this monitor. Where supported, `egressProxyUrl` should point to your dedicated listener. Also configure the proxy client's no-fallback routing yourself. Restart Claude normally if its configuration requires it, after allowing background tasks to finish.

**The initializer does not parse or validate those routing settings.** It records file hashes. A hash proves that a selected file is unchanged, not that its contents provide safe routing. Include the effective Claude proxy configuration and the effective Mihomo configuration, plus any relevant source files that can alter the route. Watching an unrelated file provides no routing assurance. Runtime proxy selections can also change without changing those files; connection sampling remains necessary.

Finder does not inherit your terminal's `PATH`. The app looks for a suitable Python in the common Apple Silicon Homebrew, Intel Homebrew, and system locations. For another installation, launch the executable with an absolute `CLAUDE_COMPANION_PYTHON` environment override. The monitor uses the same interpreter chosen by the app. Python is not bundled.

## Create the local configuration

Quit Companion before setup. From the repository directory, run the following command **after replacing every example value with your reviewed local values**:

```bash
python3 backend/network_config.py init \
  --proxy-url 'http://127.0.0.1:17897' \
  --controller-socket '/tmp/mihomo-example.sock' \
  --expected-node 'My residential node' \
  --expected-ip '192.0.2.10' \
  --watch-file '/absolute/path/to/claude-proxy-config.json' \
  --watch-file '/absolute/path/to/effective-mihomo.yaml'
```

`192.0.2.10` is a documentation-only address, not a usable proxy exit. The socket and file paths above are placeholders. Obtain the real expected exit address through a route you have independently checked; do not blindly accept a possibly unexpected result as the baseline.

If your configuration also relies on macOS system proxy settings, add:

```text
--system-proxy-url 'http://127.0.0.1:7897'
```

That optional check requires both macOS HTTP and HTTPS proxy settings to match the supplied host and port, with automatic proxy configuration and discovery disabled. Without this option, the monitor does not enforce system proxy settings. Neither form changes those settings.

Setup creates:

```text
~/Library/Application Support/Claude-Desktop-Companion/network.json
```

It creates private directories with mode `700` and the manifest with mode `600`, and refuses to replace an existing manifest. It writes no Claude, Mihomo, or system settings and makes no network request. The manifest contains your local paths, node name, expected IP and file hashes: keep it private.

Open Companion. It copies its monitoring code and manifest into its private runtime directory, runs a connection preflight, then starts an app-owned launchd job. A failed preflight leaves monitoring stopped. The app's menu reports whether monitoring is configured, stopped, checking, paused, verified, or requires attention. **备份与导入** remains available separately.

## What gets checked

| Check | Typical cadence | Effect |
| --- | --- | --- |
| Watched-file hashes; loopback listener; Mihomo controller; optional macOS system proxy | Every 1 second while Claude runs | Two consecutive local failures trigger an exit request |
| HTTPS reachability through the explicit listener | Every 10 seconds while Claude runs | Two failed probe rounds trigger an exit request |
| Claude-associated socket paths and Mihomo connection chains | About every 5 seconds while the app is open | Unexpected, unavailable or ambiguous observations prevent a verified status |
| Public IP through the explicit listener | At most once per 60 seconds while Claude runs and monitoring is active | An unexpected IP is shown as a warning |

Normal HTTPS probes send `HEAD` to `https://www.gstatic.com/generate_204`; an Apple connectivity URL is tried only if the first request fails. One probe round has a shared ten-second budget. The separate IP check fetches `https://www.cloudflare.com/cdn-cgi/trace`. Requests use an explicit proxy, disabled curl configuration, no redirect following, and TLS verification. These services can observe the proxy exit IP and request metadata. No conversation content is sent.

Local connection inspection uses process identities, `lsof`, and the Mihomo `/connections` API. It does not capture packet contents or process command-line arguments. Local status files may contain destination hosts/IPs, process paths, proxy-chain names and the sampled exit IP; do not post them without redaction.

When Claude is not running, normal external probes stop; a previously detected fault can cause recovery probes to continue. Pause stops automatic exit and new external probes, but local socket/connection inspection can continue while the app is open. The fixed proxy configuration remains yours and is not removed. Quit stops the owned monitor; there is no independent login auto-start entry.

## Exit behavior and its limits

Automatic exit is triggered by consecutive local validation failures or consecutive reachability failures. The monitor sends one `SIGTERM` to the recognized Claude main-process instance, then may signal recognized official descendants after a short grace period. It does not send `SIGKILL` to Claude, repeatedly signal the same process instance, or automatically relaunch Claude. Unrelated shell, SSH and other tool processes are not terminated merely because they descend from Claude.

A remote failure generally takes roughly **20–30 seconds** to detect and request exit; scheduling delays and shutdown behavior can make it longer. Requests are not guarantees of completed shutdown or flushed history. Save work and expect background tasks to be interrupted when monitoring acts.

**Unexpected chain or IP observations currently produce warnings, not an automatic exit request.** A reachable proxy that changes its outbound route can therefore remain running with a warning. The connection view and green/verified status must not be interpreted as a strict routing lock.

Coverage is limited to recognized Claude Desktop processes, traceable children at inspection time, and previously confirmed official Code descendants within each monitor's scope. Independent terminal Claude Code, short-lived connections between samples, unconnected UDP, untraceable orphan processes, other local forwarders and system-level tunnels may be unverified or missed. A fresh socket inspector cannot reconstruct historical ancestry. No observed connection is not proof of no traffic.

The verified status requires fresh matching monitor state, fresh usable connection observations, the configured final node, and a matching sampled exit IP. The IP probe tests one request through the listener; it does not independently verify every Claude request. Cached results are bound to the runtime configuration fingerprint.

## Verify without requesting Claude's exit

After setup, a read-only preflight can be run from the repository:

```bash
python3 backend/daemon.py \
  --config "$HOME/Library/Application Support/Claude-Desktop-Companion/network.json" \
  --check
```

This checks files and connectivity and sends external probe requests, but never signals Claude. Success confirms the specific preflight checks, not full traffic coverage or the correctness of your proxy rules. The UI's live connection inspection provides the separate chain/IP observations.

The runtime files are local:

```text
~/Library/Application Support/Claude-Desktop-Companion/
  network.json        # Reviewed trust baseline
  runtime/            # Monitor code, copied manifest, state and logs
  audit/              # Recent local connection and exit-IP observations
  history/            # Separate history feature
```

Do not copy this directory into the repository or attach it to issues. Diagnostic event logs avoid credentials and conversation contents, but observation files contain private networking metadata.

## Review and replace a changed baseline

Configuration changes are intentionally not trusted automatically. Subscription refreshes and harmless formatting changes can alter a watched file's hash and trigger the monitor. To update a baseline:

1. Quit Companion and confirm its monitor has stopped. Do not change trusted files while monitoring is active unless you intend to trigger its failure behavior.
2. Review the changed Claude/proxy configuration, route, node and expected exit IP. Fix the actual problem first; do not use reinitialization to silence an unexplained warning.
3. Preserve the existing manifest privately, without overwriting an earlier copy:

   ```bash
   python3 - <<'PY'
   from pathlib import Path
   root = Path.home() / 'Library/Application Support/Claude-Desktop-Companion'
   current = root / 'network.json'
   previous = root / 'network.json.previous'
   if previous.exists():
       raise SystemExit('An earlier private baseline already exists; preserve it before continuing.')
   current.rename(previous)
   PY
   ```

4. Rerun `network_config.py init` with the reviewed values and files. Then reopen Companion and check the resulting status.

There is no “accept all changes” or automatic baseline refresh. Keep previous manifests private too.

## Developer tests

```bash
cd backend
python3 -B -m unittest test_controller test_daemon test_watchdog_core test_flows test_network_config
```

Tests use synthetic processes, files, sockets, controller responses and signal callbacks. They do not install a real monitor or modify Claude. The production backend uses macOS `libproc`, `launchctl`, `scutil` and `lsof`; macOS CI is the supported runtime validation environment. Passing isolated unit tests on another Unix platform does not make the application supported there.
