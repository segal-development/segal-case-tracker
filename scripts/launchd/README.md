# Sync station — persistent LaunchAgents (macOS)

Runs the PJUD scraping worker on this Mac (residential IP, the only one that
passes F5 Shape) so it **survives reboots and crashes** without manual restarts.

Two agents, each a **foreground** process managed directly by launchd with
`KeepAlive` (launchd restarts them on crash or reboot):

- **`com.segal.sqlproxy`** — Cloud SQL Auth Proxy on `127.0.0.1:5433`.
  Authenticates with the user's gcloud **Application Default Credentials**
  (`~/.config/gcloud/application_default_credentials.json`), which hold the
  `cloudsql.instances.get` permission. Do **not** point it at `sa-key.json` —
  that service account is GCS-only and 403s on Cloud SQL.
- **`com.segal.syncstation`** — runs [`run-worker-foreground.sh`](../run-worker-foreground.sh),
  which sets the env (incl. `sa-key.json` for GCS uploads + the PDF-backfill
  knobs), waits for the proxy, then `exec`s the worker so the job process **is**
  the worker. (A `nohup … &` child would be reaped when the launcher exits — the
  bug that left the station dead after a reboot.)

## Why foreground + exec

launchd reaps a job's process group when the main process exits. The old
`start-sync-mac.sh` backgrounded the worker with `nohup`, so a launchd-triggered
start died immediately; it only survived when launched from an interactive shell.
Foreground + `exec` makes launchd track the worker itself.

## Install / update

```sh
# Copy the plists into place (they use absolute /Users/marcelo paths)
cp scripts/launchd/com.segal.sqlproxy.plist   ~/Library/LaunchAgents/
cp scripts/launchd/com.segal.syncstation.plist ~/Library/LaunchAgents/

launchctl unload ~/Library/LaunchAgents/com.segal.sqlproxy.plist 2>/dev/null
launchctl load -w ~/Library/LaunchAgents/com.segal.sqlproxy.plist
launchctl unload ~/Library/LaunchAgents/com.segal.syncstation.plist 2>/dev/null
launchctl load -w ~/Library/LaunchAgents/com.segal.syncstation.plist
```

## Operate

```sh
launchctl list | grep segal                 # status (col 1 = PID, col 2 = last exit)
tail -f /tmp/segal-worker.log               # worker log
tail -f /tmp/segal-sqlproxy.log             # proxy log
launchctl kickstart -k gui/$(id -u)/com.segal.syncstation   # restart worker now
launchctl unload ~/Library/LaunchAgents/com.segal.syncstation.plist   # stop worker (stays down)
```

## PDF backfill knobs

Set in `com.segal.syncstation.plist` `EnvironmentVariables`:
`DETAIL_PENDING_DOCS_FIRST=true`, `DETAIL_BATCH=80`, `SYNC_INTERVAL=2`.
Remove these three once the pending-PDF backlog is drained to return to the
normal monitoring cadence.
