# Status API

Read-only HTTP status for the put.io processor, served by `putio-statusd` so
Drovie can show live download progress — and the Drovie app behind it.

Nothing here starts, stops, pauses or otherwise touches a transfer. There is no
write surface, by design.

## Topology

```
Drovie app ──https──▶  cloudflared  ──▶  Drovie  ──▶  putio-statusd
 (iOS/tvOS)            (tunnel)          (node, :?)         (127.0.0.1:9494)
                                              │                    │
                                         owns auth           reads the files
                                                             the processor writes
```

Drovie runs on the **same machine** as the processor, so the endpoint
binds loopback only and has no auth of its own — Drovie already owns the
internet-facing boundary. Nothing off-box can reach `9494`, and nothing should
need to.

**Base URL:** `http://127.0.0.1:9494`

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/status` | Everything, transfers included. The one to poll. |
| `GET` | `/transfers` | Just the transfers array, for a hot poll loop |
| `GET` | `/healthz` | Liveness. Touches no files at all. |
| `HEAD` | any of the above | Headers only, no body |

`GET /` is an alias for `/status`. Anything else is a `404` with a JSON body
listing the valid paths. All responses are `application/json; charset=utf-8`
with `Cache-Control: no-store`.

## `GET /status`

```json
{
  "schema": 1,
  "generated_at": "2026-08-23T20:04:17Z",
  "generated_epoch": 1787515457,
  "state": "idle",
  "paused": { "active": false, "until_epoch": null, "reason": null },
  "run": null,
  "transfers": [],
  "last_run": {
    "at": "2026-08-23 16:00:03",
    "at_epoch": 1787515203,
    "outcome": "nothing to do (3s)"
  },
  "errors_in_log": 0,
  "schedule": "*/5 * * * *",
  "mount": { "path": "/Volumes/Media", "ok": true },
  "disk": { "free_bytes": 1163077455872 },
  "stale_seconds": 0.8
}
```

With a download in flight, `transfers` carries one object per transfer:

```json
{
  "name": "Some.Film.2026.2160p.WEB-DL.mkv",
  "phase": "download",
  "dest": "/Volumes/Media/Put.io Downloads/Some.Film.2026.2160p.WEB-DL.mkv",
  "size_bytes": 8461397504,
  "bytes_done": 2417542144,
  "percent": 28,
  "speed_bps": 423069875,
  "eta_seconds": 10,
  "started_epoch": 1787515471,
  "elapsed_seconds": 5
}
```

## Types

```ts
export interface PutioStatus {
  schema: number;                    // bumped only on a breaking field change
  generated_at: string;              // ISO 8601 UTC, when putioctl was run
  generated_epoch: number;           // same instant, unix seconds
  state: "idle" | "running" | "paused";
  paused: {
    active: boolean;
    until_epoch: number | null;      // null for an indefinite pause
    reason: string | null;
  };
  run: { pid: number; elapsed_seconds: number | null } | null;  // null when idle
  transfers: PutioTransfer[];
  last_run: {
    at: string | null;               // "YYYY-MM-DD HH:MM:SS", processor local time
    at_epoch: number | null;
    outcome: string | null;          // e.g. "3 downloaded, Plex scan: movies"
  };
  errors_in_log: number;             // [ERROR] lines in the retained (24h) log
  schedule: string | null;           // crontab expression, e.g. "*/5 * * * *"
  mount: {
    path: string | null;
    ok: boolean | null;         // true mounted, false NOT mounted,
                                // null the check could not run — see Failure modes
  };
  disk: { free_bytes: number | null };
  stale_seconds: number;             // age of this reading — see below
  source_error?: string;             // present only when the last refresh failed
}

export interface PutioTransfer {
  name: string;
  phase: "download" | "copy";
  dest: string;
  size_bytes: number;
  bytes_done: number | null;
  percent: number | null;            // integer 0–100
  speed_bps: number | null;
  eta_seconds: number | null;
  started_epoch: number;
  elapsed_seconds: number | null;
}
```

`GET /transfers` returns `Pick<PutioStatus, "schema" | "state" | "transfers" |
"stale_seconds">` plus `source_error` on the same terms.

## Four things to get right

### 1. Every reading is a cached snapshot — check `stale_seconds`

Handlers never touch the filesystem. A refresher thread polls `putioctl` on a
timer and handlers serialise the last good result, because a transfer in its
copy phase is measured by stat'ing the SMB share and that call sits in the
kernel uninterruptibly when the NAS drops. Reading inline would hang every
request, and Drovie behind it.

So a response is always *a* reading, not necessarily a *current* one:

- `stale_seconds` — how old it is. Under 5s is normal (refresh runs every 2s).
- `source_error` — **present only when the most recent refresh failed.** The
  data alongside it is the last good reading, deliberately not blanked. A
  momentarily unreachable share should age the data, not erase it.

A reasonable rule: show the data, and if `stale_seconds > 30` or `source_error`
is set, mark it as possibly out of date rather than hiding it.

**`stale_seconds` is not the whole age.** It is measured when the response is
built, so it says nothing about how long the response has been sitting in your
client. A consumer polling every 30s can hold a reading that says
`stale_seconds: 2` and is in truth 32 seconds old. The full age is
`stale_seconds` plus the age of your own fetch, and only you can measure the
second term — so track it, and apply the threshold to the sum. This is the term
people forget, and it matters most for the decision it feeds: a transfer
dropping out of `transfers[]` reads as "finished", and acting on that from a
reading old enough to predate the drop is how a request gets marked available
before the file has landed.

### 2. Read `transfers` directly — do not gate on `state`

`state` reports whether the **processor** is running, which is a separate
question from whether transfers exist. They move together in practice, but they
are independent fields and a test fixture can produce transfers with
`state: "idle"`. Render whatever is in `transfers`.

### 3. Progress is null for the first few seconds

A transfer is published *before* the first byte moves, so it appears in the
array immediately — but rclone only emits stats every `RCLONE_STATS_INTERVAL`
(default 5s). Until the first tick:

```
bytes_done: null, percent: null, speed_bps: null, eta_seconds: null
```

`name`, `size_bytes` and `started_epoch` are there from the start. Show an
indeterminate bar rather than `NaN%`.

The same applies in `phase: "copy"` — the staged copy onto the media share is a
plain `cp`, which reports nothing, so progress is inferred from the size of the
file appearing at the far end. **`speed_bps` and `eta_seconds` stay `null` for
the whole copy phase.** A large file can sit in `copy` for minutes; it is not
stalled.

### 4. Idle keep-alive connections are closed after 30s

The server speaks HTTP/1.1 with keep-alive, but closes a connection that sits
idle for 30 seconds. A pooled client (undici, which is what `fetch` uses in
Node) handles this correctly on its own — this is noted only so a reset socket
in a long-lived pool isn't mistaken for a fault.

## Failure modes

| What you see | Means | Do |
|---|---|---|
| `200` + `source_error` | Last refresh failed; data is the last good reading | Show it, flag as stale |
| `200` + high `stale_seconds` | Same, or the refresher is slow | As above |
| `503` `{"error": "no reading yet"}` | Daemon just started, first refresh hasn't finished | Retry shortly |
| `ECONNREFUSED` | Daemon not running | Surface as "status unavailable"; the sync itself is unaffected |
| `404` | Wrong path | Body lists the valid ones |
| `mount.ok === null` + `mount.path` set | The probe itself could not run — worth alerting on. Not an assertion that the share is down | Show "unknown", never "not mounted" |
| `mount.ok === null` + `mount.path === null` | No mount point is configured. Expected, not a fault | Show nothing, or "not configured" |

The daemon is a pure reader. If it is down, wedged, or never installed, **the
sync keeps working normally** — treat its absence as a missing view, never as a
problem with downloads.

`mount.ok` is three-valued for a reason. It was briefly two-valued in practice,
and the missing third state cost a day: under launchd the daemon inherits no
`PATH`, `mount` (in `/sbin`) could not be found, and the failed lookup was
reported as `false` — a healthy share drawn as **NOT MOUNTED** in Drovie
for as long as nobody checked. Render `null` as unknown. A consumer that treats
falsy as "down" reintroduces exactly that bug.

The two `null` cases are told apart by `mount.path`, as the table above shows:
a configured path with `ok: null` is a broken probe, and a null path is simply
an install that never set `MEDIA_MOUNT_POINT`. Only the first deserves an alert.

## Polling

Refresh runs every 2s server-side, so polling faster than that returns the same
snapshot. Once every 2–5 seconds while a user is watching a transfer, and much
less often otherwise, is plenty. Set a client timeout anyway — 2s is generous:
responses come from memory, measured over loopback at a mean of 1.7 ms and a
worst case of 14.7 ms, and that holds while the source is wedged because the
handler never waits on it.

## Versioning

`schema` is `1`. **Fields will be added without a bump** — treat unknown fields
as ignorable. `schema` increments only when an existing field changes meaning or
disappears, so pin your parsing to the fields you use and check `schema` before
trusting a field's semantics. The Drovie app ships via TestFlight and old builds keep
polling long after the server has moved on, which is why this exists from day
one.

## Building against it without waiting for a download

The repo ships a generator that publishes the same files a real transfer does,
so the endpoint reports a transfer that isn't happening. Nothing real is
touched — no put.io, no rclone, no media share:

```bash
tests/fake_transfer.sh "Some.Film.2026.2160p.mkv" 8461397504 120
```

That runs a fake 8 GB transfer over 120 seconds; poll `/transfers` and watch
`percent` climb. Useful for building the UI, and for exercising the null-progress
window in point 3.

To see the JSON the daemon is reading, without the daemon in the way:

```bash
putioctl status --json | jq .
```

## Why the daemon shells out

`putio-statusd` runs `putioctl status --json` on a timer and serves the last
result. That indirection looks removable now that the processor itself is
Python — phase 4 of the port pencilled in "statusd calls the shared module
directly" — and phase 5 reconsidered it and **kept the shell-out**.

`putioctl` is still bash, and it is the thing that discovers *which*
`.putio_config` applies and then sources it. `.putio_config` is a bash file:
values reference `$HOME`, and nothing stops one using command substitution. For
the daemon to skip `putioctl` it would have to either re-implement bash's
semantics in Python — the one thing this port has refused to build from the
start, because it would mean two readers of one file that must agree forever —
or shell out to bash anyway.

Meanwhile the problem a direct call was meant to solve is already solved where
it matters. The danger was never the fork; it was a reader wedged on a dropped
SMB share taking Drovie and the Drovie app down with it. So a refresher thread
owns the snapshot, **no request handler ever touches the filesystem**, and a
timed-out `putioctl` has its *process group* killed and is then abandoned
rather than waited on — because waiting on something in uninterruptible sleep
is the wedge being avoided.

Revisit this only if `putioctl` itself is ever ported.

## Related

- `PUTIOCTL.md` — the CLI, including installing and restarting the daemon
- `CLAUDE.md` — why the daemon is shaped the way it is
- `putio-statusd` — the implementation, ~350 lines of stdlib Python
