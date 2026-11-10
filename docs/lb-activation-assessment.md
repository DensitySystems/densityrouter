# Load-balancing activation assessment

Assessment only (step 1). Written against `main` at the time of this branch. Nothing here turns live mode on, merges a pull request, or deploys.

Scope is activation: how the balancer is turned on, how harness accounts become eligible, health and readiness, failover when a backend errors or rate-limits, timeouts and retries, session handling, the config and environment flags, and the Grok Bot client path (endpoint, handshake, request and response shape).

Out of scope: the score formula, task-type keywords, LAN role policy, and any new routing policy. `THROTTLE_PCT` stays unset. There is no local model tier and this design does not add one. Each CLI keeps its own account. Pools stay separate.

## Verdict

The balancer is the live walk in `live.py`. It sends a packet only when live is on, then tries ranked harnesses and moves to the next candidate on four failure statuses. There is no second "load balancer" switch, no runtime registry, and no network listener.

On current `main`, a Grok Bot on the same machine can complete a call by writing a markdown packet, a matching GO ledger line, and a RUNS row, then running `live.py` with `DISPATCH_LIVE=1` and reading the JSON on stdout. That path still fails a flawless bar: a rate-limited account is chosen again on the next call, the Grok session can sit for 600 s before failover, a successful reply that contains words such as `credits` or `429` is treated as a usage failure, an unexpected exception skips failover, and the reply text the bot sees is cut to 4000 characters (Grok session) or 2000 characters (other harnesses).

[PR #1](https://github.com/DensitySystems/densityrouter/pull/1) (drop the GO-ledger and RUNS-row refusal) is open and unmerged. Turning the balancer on does not depend on it. A client that sends a packet and expects a harness call without those two files does depend on it. See [PR #1](#pr-1).

The final plan, revised after three REVISE reviews, is [hybrid scripted design](hybrid-scripted-design.md). This file stays the code assessment of what the router does today.

## Current state

Stdlib Python. The process that runs `live.py` or `dispatcher.py live` is the whole router. Default is offline: zero CLI calls.

### How the balancer is turned on

| Switch | Where | Effect |
|---|---|---|
| `DISPATCH_LIVE=1` | environment of one process | Live for that process only. Any other value is off. |
| `<state-dir>/LIVE` | empty file | Live for every process that uses that state dir. |
| `python3 live.py --off` | `live.rollback_off` | Removes the LIVE file. If `DISPATCH_LIVE=1` is set, the message says that flag must be unset. The flag is left set. |
| `<code-dir>/KILL` or `$DISPATCH_KILL` | `dispatcher.check_kill` | Every command exits 3 before a call. KILL wins over live. |

`live.live_enabled` is the check (`live.py`). `dispatch --dry-run` never calls a CLI, including when live is on.

There is no config key named balancer, activation, or enabled-for-routing. `caps.json` `enabled` is loaded and not consulted (`dispatcher.route`). `usage_layer.THROTTLE_PCT` is `None`. `dispatcher.HEALTH_COOLDOWN_S` is `None`.

`handoff.balancer_choice` is set only when the operator passes `--harness` (`live.run`). An unpinned call's first choice is `handoff.would_send_to`. The harness that actually returned is `call.harness` after the walk.

### How a harness account becomes eligible

Registration is the config file plus `capability-map.json`. Nothing registers at runtime. `drconfig.load` caches the file by mtime, but these names are fixed when the modules import:

- `dispatcher.FALLBACK` = harnesses with `route: true`, in file order
- `harness_call.BINS`, `INVOKE`, `HARNESS_TIMEOUT`, `ASYNC`
- `usage_layer.POOLS`, `PARSERS`, `REQUEST_ARGV`

A one-shot `live.py` process reads the config at start. A long-lived process keeps the imported lists until it is restarted.

A harness is a live candidate when all of the following hold:

1. `harnesses.<name>.route` is true (`drconfig.routed`). `route: false` is `ROUTE_EXCLUDED`: logging only, and a pin to it exits 7.
2. The packet is not LAN, or this harness is `lan.harness`. With `lan.harness` null, a LAN packet ranks nobody and the live run HOLDs.
3. Health cooldown does not skip it. With `HEALTH_COOLDOWN_S` unset, a recorded failure does not skip. `route` reports `health_gap` when the health file has a failure class and cooldown is unset.
4. `capability-map.json` has a number for this harness and the classified task type. A missing ability makes `score` null. The walk keeps only rows whose `score` is not null (`live.run`). A harness with no ability is left out whenever any other harness has a score.
5. At call time, `invoke` is a non-empty argv and the binary can be started. `invoke: null` yields `NOT_DISPATCHED`. Unpinned, that status is walked (`LIVE_WALK`). A pin tries only the pin and exits 6.
`route()` does not read `pool`. The field names the usage meter on the ledger (`usage_layer.HARNESS_POOL`). `usage_layer.POOLS` has one entry per routed harness, plus `log_only_pools`. Two entries that reuse one `pool` string write one meter name. Give each account its own `pool`.

`account` and `subscription` are labels. They do not select a login. Two harness entries that share `bin`, the OS user, and `run_host` share one CLI login and one real usage meter even if their `pool` strings differ. A second account is a separate OS user, or `run_host.mode: "ssh"` to a host where that CLI is already logged in to the other account. The router never logs in, never reads credentials, and never installs a CLI. The operator's real config may list more than the three example harnesses. Tests pin `DENSITYROUTER_CONFIG` to `tests/fixtures/test.config`, whose routed names are `grok-build`, `cursor`, and `agy-gemini`, and they reject a harness whose name contains `local` (`tests/test_pool_membership.py`). That fixture is not a cap on the operator config.

`caps.json` (`enabled`, `daily_cap`, `used_today`, `test_call_cap`) never adds or removes a candidate.

Shipped example (`densityrouter.config.example`): `grok-build`, `cursor`, and `agy-gemini`, each `route: true`, `async: false`, `run_host.mode: local`, its own `pool`. `grok-build` and `agy-gemini` set `timeout_s` null. `cursor` sets `timeout_s` 180.

### Health and readiness

`harness_call.record_health` writes `<state-dir>/health.json` as `{harness: {class, ts}}` for `done` and for `FAIL_CLASSES` (`failed_retryable`, `timed_out`, `failed_hold:usage`, `failed_hold:not_installed`, `cancelled`, `STALE`). The write is atomic (tmp file plus `os.replace`). A later `done` overwrites the failure row.

`dispatcher.health_skip` skips a harness only when cooldown N is a number, the row's class is a failure class, and `now < ts + N`. The production `route()` call uses `HEALTH_COOLDOWN_S`, which is `None`, so H stays 1.0 and nobody is skipped. A caller that passes its own health dict (tests) uses that dict as H. The file path does not.

There is no readiness gate in `route()`. These checks happen only when a call is attempted:

- Cursor, local: missing binary returns `failed_hold:not_installed` before the subprocess. Stderr containing `not logged in` with a non-zero exit adds `reason` and still uses `exit_map` (a non-zero exit with no quota hint is `failed_retryable`).
- Any harness: `FileNotFoundError` maps to exit 127, then `failed_hold:not_installed`.
- Grok session: a trust prompt raises `TrustPromptRefused`. `live.run` catches that, records a session error, and maps it through `exit_map` (non-zero, so `failed_retryable` unless the error text hits a quota hint).

`python3 usage_layer.py read <harness>` reads that CLI's `/usage` screen and does not send a model prompt. It is the existing readiness probe. Its result is not an input to eligibility.

Usage snapshots affect the score's U term. They do not by themselves take a harness out of the walk. No snapshot (or no usage dir) yields U = 0.3, state `unknown`, and a score is still produced when ability is set. An empty usage dir whose writer declarations are unset yields U = 0, state `config-gap`. When every scored harness is in that gap, `route` scores on ability only.

### Failover

`dispatcher.WALK_STATUSES` is `failed_retryable`, `timed_out`, `failed_hold:usage`, `failed_hold:not_installed`. Unpinned live runs also walk `NOT_DISPATCHED`. The walk stops on `done`, `PENDING`, `cancelled`, `REFUSED`, `STALE`, and every other status.

`harness_call.exit_map` classifies one attempt:

- timeout → `timed_out`
- stdout plus stderr contains any of `quota`, `rate limit`, `429`, `resource_exhausted`, `usage limit`, `credits` → `failed_hold:usage`
- exit 0 → `done`
- exit 124, 137, -9, -15 → `cancelled` (stops the walk)
- exit 126 or 127 → `failed_hold:not_installed`
- anything else → `failed_retryable`

The walk starts at `would_send_to` and continues down the scored list. It does not retry the same harness. It does not return to a higher-ranked harness. A row with a null score is not in the walk. If every score is null (no ability numbers) and the GO line and RUNS row exist, live still passes the dry-run check and then walks nobody, returning exit 6 with status `ROUTED LIVE -> None (None)`. The shipped capability map has a number for every task type, so this shows up only when a map row is missing. Each attempt takes a lease (`dispatcher.lease_acquire`, default TTL 600 s) and releases it before the next harness, except a real `PENDING`, which extends the lease to the pending deadline (default 30 min, `ASYNC_DEADLINE_MIN`).

The same packet bytes always produce the same job id: first 16 hex chars of SHA-256 of `file-sha256 + ":run"` (`dispatcher.dry_run`). A second process that submits those same bytes while the lease or an unexpired pending file is held gets no ranking (`ranked` empty → HOLD, exit 2) or, if it loses the race at acquire time, exit 5.

A failure row in `health.json` does not change the next call's ranking while cooldown is unset. The same account can be first choice again immediately.

`cancelled` does not fail over. Tests lock that (`tests/test_failover_lease.py`).

### Timeouts and retries

| Path | Limit | Code |
|---|---|---|
| `harness_call.call` default | 60 s | `TIMEOUT`, used when `timeout_s` is null or absent |
| `cursor` | 180 s | `harnesses.cursor.timeout_s` |
| Grok live session `ask` / `grok_session_call` | 600 s | hardcoded defaults. `timeout_s: null` on `grok-build` is not read here |
| `usage_layer.run_request` | 300 s | direct one-harness path, not the walk |
| `/usage` read beside a live call | 45 s | `live._async_read.READ_WAIT_S`, log-only, does not fail the call |
| Grok `/usage` inside the session | 20 s pump | `GrokSession.capture_usage` |
| `grok sessions list` / `grok usage` | 30 s each | `grok_session_usage` |
| Session boot | 9 s | `GrokSession.boot_s` |
| Lease TTL | 600 s | released on every non-PENDING result |
| Async pending deadline | 30 min | shipped harnesses are `async: false`, so this path stays idle |

There is no retry count, backoff, or second attempt on the same account. The walk to the next account is the only retry.

`subprocess.run` in `harness_call.call` kills the direct child on timeout. It does not kill the process group. `usage_layer.run_group` does kill the group, and that function is the `usage_layer.py run` path, not `live.py`. After a non-session live call, `reap_cwd` signals stray processes whose cwd is `state/jobs/<job>`.

The Grok session timeout returns the string `timeout` and leaves the TUI running. `exit_map` then yields `timed_out`, which does walk. The session is fenced only when the post-call usage read times out, not when `ask` times out.

### Session and stream handling

`live.run` sends `grok-build` through one process-global `GrokSession` (`usage_layer.grok_router_session`). The first call's cwd is kept for the life of the process. Later calls do not pass a new cwd into an existing session.

The session is a pty. It bracket-pastes the whole packet and waits until the screen matches `Worked for <digit>`. Finish marker, then exit 0, means `done` unless a quota hint is also in that screen text. The reply the bot sees is, in order: the last assistant message in that session's `chat_history.jsonl` when `grok usage` verifies the session id, otherwise `clean_tui_reply` of the screen. `live.run` stores at most the last 4000 characters in `stdout`. `harness_call.call` stores at most the last 2000 characters of stdout and of stderr.

`usage_layer.py run` records the caller's `--bot-id`. The live walk records bot id `router` (`live.run` → `make_row`). The bot id does not choose a harness.

No stdout is streamed. The CLI prints one JSON document after the walk finishes (`json.dumps(..., indent=1)`). Stderr may carry the cursor `agent_guard` warning. The client parses stdout only.

A Grok session object lives only inside one process. Each new `live.py` process boots a new TUI (about 9 s) and drops it when the process exits. Two overlapping `live.py` processes for the same account start two TUIs. There is no lock file.

Harnesses whose name is not `grok-build` never enter `GrokSession`, even if `usage.parser` is `grok`. They use `harness_call.call` and the `invoke` argv. Session-token verification (`grok_session_usage`, `parse_grok_session` saved off a usage screen) is also name-gated on `grok-build`.

Cursor's `invoke` places the packet after `--`. `forbidden()` stops at `--`, so packet text is not treated as a flag (`tests/test_ca_advisories.py`). Agy's `invoke` is `{bin} -p {prompt}` with no `--`. `forbidden()` tokenizes the packet. A packet that contains the tokens `--force`, `--yolo`, `-f`, `--mode`, or `--dangerously-skip-permissions` makes agy return `REFUSED`. `REFUSED` stops the walk.

### Config and environment flags

Flags that change whether a call is sent:

| Name | Enables |
|---|---|
| `DENSITYROUTER_CONFIG` | Which file supplies harnesses and paths. Missing file → exit 9. The `.example` is never used silently. |
| `DISPATCH_LIVE`, `<state-dir>/LIVE` | Live on, as above. |
| `DISPATCH_STATE` | State dir for LIVE, leases, pending, `health.json`, `calls.json`, `dispatch.log`, `usage-ledger.jsonl`, `jobs/`. |
| `DISPATCH_KILL` | KILL file path. |
| `harnesses.<h>.route` | Eligibility. |
| `harnesses.<h>.invoke` and `bin` | A callable argv. Null invoke → `NOT_DISPATCHED`. |
| `harnesses.<h>.run_host` | `local` or `ssh`. This is where that account's CLI runs. |
| `harnesses.<h>.pool` | Usage-meter name. Must be that account's own pool. |
| `harnesses.<h>.timeout_s` | Seconds for `harness_call.call`. Null means 60 s there. Ignored by the Grok session path. |
| `harnesses.<h>.async` | `true` submits `PENDING` and does not run the sync timeout. Shipped entries are `false`. |
| `harnesses.<h>.usage.parser` | `grok`, `cursor`, or `agy`. Required for a `/usage` read. Not required to be called. |
| `harnesses.<h>.usage.reader_cwd` | Directory the CLI already trusts, used for `/usage`. |
| `capability-map.json` row | Ability. Required for a non-null score. |
| `lan.harness` | The only harness for a LAN packet. Null means a LAN packet is HOLD. |

Flags that do not enable or disable a call: `caps.json` `enabled` and the cap counters, `account`, `subscription`, `owner`, `off`, `desktop_only`, `host`, `route_via`, `usage.source`, `usage.report_step`, `THROTTLE_PCT`, `HEALTH_COOLDOWN_S` while it stays `None`, `--non-critical` on `usage_layer.py run` (recorded only).

`GROK_BUILD_BIN` and `CURSOR_AGENT_BIN` override `bin` only when the value is an absolute path of an allowlisted binary. They are not an activation switch.

### Grok Bot client path (as the code stands)

There is no URL, no socket, and no `Authorization` header. The endpoint is a local command, run as the OS user that owns the CLI logins:

```
DISPATCH_LIVE=1 DISPATCH_STATE=<state-dir> python3 live.py <packet.md> --ledger <go-ledger> --runs <runs-file>
```

`dispatcher.py live` is the same `live.run`. Do not use `dispatcher.py dispatch --dry-run` (never calls). Do not use `usage_layer.py run` for a balanced call: it requires a harness name and skips the walk. Do not pass `--harness` (pin, no failover, exit 7 when the pin is ineligible).

Handshake on current `main`, before the command:

1. Write the packet as UTF-8 markdown. The task type is read from an inline `Objective:` line. A `## Objective` heading is not that line.
2. SHA-256 the file. The GO line must contain `| GO |`, the packet basename, and the first 12 hex chars of that digest (sha12).
3. The RUNS file must contain a line with the packet path or the basename.
4. Both files must exist. A missing file raises `FileNotFoundError` from `find_go` / `find_runs` (the process errors; it does not return exit 2). An existing file with no matching line makes `dry_run` not ok, and live returns exit 2 with `HOLD (live: dry-run checks failed, nothing sent)` and zero calls.
5. Editing the packet changes sha12. The GO line has to be rewritten to match.
6. The packet text must not match the redaction patterns (bearer tokens, private keys, common key prefixes, long hex). A hit returns exit 8 and sends nothing. There is no failover.

The three shipped harnesses are synchronous. Success is exit 0, `call.status` of `done`, and the reply in `call.stdout` (redacted, truncated). `handoff.walked` lists attempts. `handoff.would_send_to` is the first choice and can differ from `call.harness` after a walk. `call.status` of `PENDING` is also exit 0; the shipped config does not produce it.

Other exits: 2 HOLD, 3 KILL, 5 lease refused, 6 the walk finished on a non-success status (including every candidate failing), 7 pin refused, 8 packet redaction, 9 config missing.

The response is this router's JSON, not a chat-completions body. There is no `choices` array. A client that speaks only that shape will not round-trip.

## PR #1

[https://github.com/DensitySystems/densityrouter/pull/1](https://github.com/DensitySystems/densityrouter/pull/1)

- Title: Drop GO-ledger and RUNS-row refusal
- State: open, mergeable, not merged
- Head branch: `drop-go-runs-gate-20261031`
- Head commit: `08ed378017486eeeb53519833bcacf8b9aa975f7`
- Base: `main`

What that branch changes in the call path: `dry_run` no longer opens a ledger or a RUNS file. `ok` becomes "the ranking is non-empty". `--ledger` and `--runs` stay on the command line and are ignored. `live.run` drops those two parameters. KILL, the LAN rule, leases, secret-pattern refusal, and the live switch stay.

What it does not change: `DISPATCH_LIVE`, harness `route`, health records, `HEALTH_COOLDOWN_S`, `WALK_STATUSES`, `exit_map`, timeouts, the Grok session, reply truncation, or pools.

Dependency:

- Activating live mode, listing accounts in config, recording health, and walking failures does not require this PR.
- A Grok Bot that writes a packet and runs `live.py` without a matching GO line and RUNS row gets exit 2 and zero CLI calls on `main`. That shortcut requires this PR, or the bot must keep doing the file handshake above.
- This assessment does not merge the PR. Later code for timeouts, cooldown configuration, quota matching, and reply text can land on `main` without it. A bot build that omits the handshake waits until the PR is merged.

## Gaps that block flawless calls

1. **Approval files on `main`.** Every new packet needs a GO line whose sha12 matches the bytes, and a RUNS row. A missing file is an exception, not a clean HOLD. This is the client handshake until PR #1 lands.
2. **Sick accounts stay first choice.** Health is written and then ignored for eligibility, because cooldown has no config or env override. A rate-limited account is tried again on the next packet and the bot pays another full timeout before the walk.
3. **Grok failover waits 600 s.** The session timeout ignores `timeout_s`. A hung TUI blocks the bot for 600 s, then the walk continues. `ask` does not fence the session on that timeout.
4. **Quota hints run on the reply body.** `exit_map` scans stdout (and, for `harness_call`, stderr). A finished answer (exit 0, or a Grok screen that also matched `Worked for`) that contains `credits`, `429`, `quota`, `rate limit`, `usage limit`, or `resource_exhausted` becomes `failed_hold:usage`. The walk continues and the bot receives a later harness's reply, or exit 6. The first account has already done the work.
5. **`REFUSED` and uncaught errors do not walk.** Agy tokenizes the packet, so a coding packet that mentions `-f` or `--mode` refuses agy and stops the walk. `harness_call.call` does not catch `OSError` (argument list too long, permission errors). That exception leaves `live.run` entirely; later candidates are not tried. The Grok session path does catch exceptions and maps them to a retryable status.
6. **The bot does not see the full reply.** Grok live stdout is sliced to 4000 characters. Other harnesses slice to 2000. Redaction still replaces secret-shaped spans with `[REDACTED]` inside a `done` result (exit 0). The bot cannot tell a cut reply from a short one except by length.
7. **No single-flight per account.** Overlapping `live.py` processes share a login and start two Grok TUIs, or two cursor processes, on one account. The in-process session is not shared across processes, and the first cwd sticks for the life of one process.
8. **Second Grok account bypasses the session path.** Only the harness name `grok-build` uses `GrokSession` and the verified session usage read. Another name with `usage.parser: grok` is a one-shot argv call. That split is easy to mis-configure when each CLI has its own account.
9. **No readiness input to the walk.** A CLI that is not installed or not logged in is discovered on the attempt. Cursor's missing binary fails fast. A trust prompt or a login failure still occupies that attempt. `usage_layer.py read` can show this beforehand and is not wired to anything the bot runs.
10. **Client shape.** No HTTP API exists. A bot that posts a chat-completions JSON body has nothing to post to. `usage_layer.py run` looks like a request API and is not the balancer. Success JSON uses `call.stdout` and exit 0, with `bot_id` fixed to `router`.

## Recommended activation design

Keep the local process as the endpoint. Do not add a listener, a shared account pool, a throttle, or a local model harness. Do not change score weights.

### On switch

Use `DISPATCH_LIVE=1` on the Grok Bot's router process, with that process's own `DISPATCH_STATE`. Leave the shared `<state-dir>/LIVE` file absent so other dry runs stay offline. Keep the KILL file absent. Turn a process off by unsetting `DISPATCH_LIVE`. Use `live.py --off` only to remove a LIVE file someone created.

The bot invokes `live.py` or `dispatcher.py live` once per packet, with no `--harness`.

### Eligibility

One config entry per CLI account:

- Unique harness name, `route: true`, its own `pool` string.
- Its own login: different OS user, or `run_host.mode: ssh` with `BatchMode` to a host where that CLI is already logged in. `account` stays a label.
- A `capability-map.json` row with a number for each task type it may receive.
- A `caps.json` object as a label only. Leave `enabled` unused as a switch.
- `invoke` set. For a Grok-parser account, the same session behavior as `grok-build` (implementation step 6), still one session per harness, not a shared pool.
- `usage.reader_cwd` set to a directory that CLI already trusts, so the first live call does not trip the trust prompt.
- `async: false` for these CLIs, so the bot gets `done` in the same process.
- `timeout_s` set to an explicit number of seconds on every entry, including Grok. The session path must use that number (implementation step 2).

Before flipping `route` to true, run `python3 usage_layer.py read <harness>` and require `parse_ok` true. That is the readiness check. It does not send a model call. A failed read means the account stays `route: false` until the CLI is installed and logged in. Call-time `failed_hold:not_installed` remains the backup and still walks.

Config edits apply to the next process. The bot's one-shot command gets that for free. Do not keep a long-lived router process until import-time lists are re-read on each `run()` (not required for the one-shot path).

### Failover

Keep the current walk statuses and the rule that `cancelled` stops the walk (an operator kill of the CLI is not a reason to spend the next account).

Add three activation behaviors:

- A config/env cooldown, default unset, so today's tests and today's "H = 1.0" behavior stay the default. When the operator sets `health.cooldown_s` (seconds), `route` skips a harness whose latest health row is a failure class younger than that many seconds. A later `done` already overwrites the row, so a success ends the skip on the following call. This is eligibility after an error, not a throttle: it does not slow a healthy account and it does not read `THROTTLE_PCT`.
- Quota hints apply to CLI failure output, not to a successful reply. Exit 0 with those words in the assistant text stays `done` and does not walk. A non-zero exit or a timeout whose error text contains a hint stays `failed_hold:usage` and walks.
- `OSError` from the subprocess, and agy `REFUSED` caused by packet text rather than an operator flag on the argv template, become walkable. The practical fix for agy is the same `--` separator cursor already uses, so packet tokens are not flags. `REFUSED` for an actual forbidden flag on the template stays a stop.

One attempt per account per packet. No second try on the account that just failed.

### Timeouts

Every live attempt, including the Grok session, uses that harness's `timeout_s`. On session timeout, close or fence the session before the next candidate so the next prompt is not typed into a live generation. Pick the number from the bot's deadline divided across the candidate list. The current 600 s default is why a single hung Grok account dominates the bot's wait; set a shorter explicit value when failover latency matters. Cursor's 180 s and the 60 s default for a null `timeout_s` stay as they are for `harness_call`.

`usage_layer.py run` stays a manual one-harness tool. The bot does not call it.

### Sessions

One Grok TUI per harness name per process, selected by `usage.parser == grok` rather than by the single name `grok-build`. A state-dir lock per harness serializes accounts: a second call for that same harness waits a short, bounded time and then walks to the next candidate if the lock is still held. Different harnesses (different accounts) run together.

One-shot `live.py` remains valid. Each process may boot a TUI, pay the 9 s boot, and exit. That is compatible. The lock is what makes overlapping bot calls safe. Call `close` on the session when the attempt finishes, including timeout, so the account is not left mid-prompt.

No streaming in this design. The bot waits for the JSON. The worst wait is the sum of the per-harness timeouts along the walk.

### Reply and ledger

Put the full redacted reply in `call.stdout`. Keep writing `state/jobs/<job>/` as the work directory. Redaction of the reply stays; exit 0 with `redaction_hits` greater than 0 means the text was masked, and the bot should not resubmit the same packet in a loop.

Optional and not required for a correct call: accept a bot id on the live command and store it on the usage-ledger row. Routing ignores it. Until that exists, every live row says `router`.

### Client contract to implement against

Request:

1. Packet file, UTF-8, with an inline `Objective:` line. Keep the token `LAN` off the Objective line and off `Placement` / `Hosts` lines unless `lan.harness` is set; otherwise the ranking is empty and the call HOLDs. `Placement: local` is not a LAN packet.
2. On `main`: GO line and RUNS row as in the handshake above. After PR #1 merges: those files are unnecessary, and passing the flags is harmless.
3. Command as above. Environment: `DISPATCH_LIVE=1`, `DISPATCH_STATE` set, `DISPATCH_KILL` unset, `DENSITYROUTER_CONFIG` pointing at the filled-in config.
4. No router bearer token. The OS user is the trust boundary. Each CLI is already logged in on its `run_host`.

Response:

- Parse all of stdout as one JSON value. Ignore stderr for the result.
- Exit 0 and `call.status == done`: use `call.stdout` as the reply and `call.harness` as the account that produced it.
- Exit 0 and `call.status == PENDING`: only if a harness is `async`. Not the shipped CLIs.
- Exit 2: nothing was sent (handshake, empty ranking, or live dry-run failure).
- Exit 5: this packet's job id is already leased. Wait for that attempt; do not start a second one with the same bytes.
- Exit 6: every attempted account failed. Wait before submitting again. The router will not insert that wait; the client does. That wait is not the throttle phase and must not be built as `THROTTLE_PCT`.
- Exit 3, 8, 9: fix KILL, the packet text, or the config. Do not walk those by retrying.

Identical packet bytes share a job id. A retry after exit 0 should be a new packet (new bytes) when the bot wants a distinct job.

## Implementation plan

Each step is independent enough to land alone. Steps 2–7 are what make the client path flawless. Step 1 is the contract test that freezes today's JSON so later steps cannot silently change it. None of these steps merge PR #1 or set `THROTTLE_PCT`.

1. **Lock the client contract in a test.** Drive `live.run` with a fake `call_fn`, a temp state dir, `DISPATCH_LIVE=1` passed only in the env dict (the existing suites never put it in `os.environ`), a GO line, and a RUNS row. Assert exit 0, `mode == live`, `call.stdout` equal to the fake reply, `call.harness` equal to the walked winner, and `bot` ledger id `router`. Assert a second case with no GO line returns 2 and does not call. Assert `--harness` is absent from this test. This test describes the handshake a Grok Bot must perform on `main`.
2. **Honor `timeout_s` on the Grok session path.** Thread the harness timeout into `grok_session_call`. On timeout, fence the session and return `timed_out`, then let the existing walk continue. Add a test with an injectable session that never emits `Worked for`. Assert the attempt status is `timed_out` and the next fake harness returns `done`. Set an explicit `timeout_s` on `grok-build` in the example config and in `tests/fixtures/test.config`, and keep `tests/test_cursor_invoke.py`'s "cursor 180 s, default 60 s" expectation true for `harness_call`.
3. **Cooldown as config, default off.** Read `health.cooldown_s` from the config, overridable by `DISPATCH_HEALTH_COOLDOWN_S` for one process. Absent or null keeps `HEALTH_COOLDOWN_S is None`, so `tests/test_health_cooldown.py`, `tests/test_pool_membership.py`, and `tests/test_scenario_table.py` stay valid. When the operator sets a positive integer, `route` uses the existing `health_skip`. Add a test that the default is null and that a set value skips a fresh `failed_hold:usage` row and still scores the other harnesses. Do not skip on `done`.
4. **Quota match on failures only.** Change `exit_map` so the hint list is applied when the attempt did not finish cleanly (non-zero exit, timeout, or session error), and a clean exit 0 stays `done` even if the reply contains `credits` or `429`. Update any test that expected the old scan of a successful body. Add a test: fake stdout `done` text containing `credits` does not walk; fake exit 1 stderr `rate limit` does walk to the next harness.
5. **Walk real call failures.** Put `--` before `{prompt}` on agy, matching cursor, and test that a packet containing `-f` and `--mode` is `done` on a fake agy binary rather than `REFUSED`. Catch `OSError` in `harness_call.call`, record `failed_retryable`, and test that `live.run` then calls the next harness. Leave template-level forbidden flags (`--force` on the argv before `--`) as `REFUSED` that stops the walk.
6. **One session and one lock per Grok-parser harness.** Key the session by harness name for every harness whose parser is `grok`. Take an exclusive lock file under the state dir for that harness around the attempt; if the lock is not acquired within a few seconds, treat the attempt as `failed_retryable` and walk. Release the lock in `finally`, and close the session on timeout. Test two overlapping fake calls on the same harness: the second walks. Test two different harness names: both proceed. Keep pools separate. Do not add a `local` harness.
7. **Full redacted reply.** Stop slicing `stdout` at 4000 and 2000. Add a test that a 5000-character fake reply is returned intact, and that a bearer-shaped span inside it is still `[REDACTED]` with exit 0. Confirm `tests/test_no_hardcode.py` still passes (no new paths, hosts, or internal ids in shipped files).
8. **Optional bot id.** Add an optional `--bot-id` on `live.py` that is stored by `make_row` and ignored by `route`. Default remains `router`. Test that two ids produce two ledger rows and the same harness choice.

After step 1 the bot can be wired to current `main` using the handshake. After steps 2–7 the same client survives a hung Grok account, a rate-limit, a packet that mentions `-f`, and a long reply. Merge PR #1 only when the owner wants the handshake removed; then delete the GO/RUNS arrange step from the bot and keep the contract test's "no GO line" case aligned with that branch (exit 0 when the ranking is non-empty, instead of exit 2).

### Test plan for this assessment

This branch adds no router code. The existing suite is the baseline:

```
python3 -m unittest discover -s tests
```

Result on this branch, before the doc commit: **Ran 286 tests in 62.487s, OK**. 0 failures, 0 errors, 0 skips. That matches the count documented in `README.md` and `USAGE.md`.

Later implementation steps re-run that command and add the tests named in the step. Suites that must stay green without edits unless the step says otherwise: `tests/test_failover_lease.py` (walk statuses, `cancelled` does not walk), `tests/test_health_cooldown.py` (default cooldown unset), `tests/test_live_wiring.py` (live flag exact match), `tests/test_pool_membership.py` (three harnesses, no `local`, `enabled` is not a skip), `tests/test_no_hardcode.py`, `tests/test_scenario_table.py` (`DISPATCH_LIVE` never set on `os.environ`).

## Risks

- **PR #1 drift.** Building the bot against `main` and then merging PR #1 changes `live.run`'s signature and drops the handshake. The bot and step 1's test have to move together with that merge. Merging it early, before the owner accepts it, is the larger risk; this assessment leaves it unmerged.
- **Cooldown hides an account.** A value that is longer than the CLI's real reset window keeps a healthy-again account out of the ranking. The default stays unset so shipping the config key does not change behavior. The operator sets the seconds deliberately.
- **Quota-on-failures-only can miss a polite refusal.** A CLI that exits 0 while the screen is a rate-limit message with no hint in a separate error channel would stay `done`. The current hint list on the reply body catches that and also catches ordinary coding answers. The recommended rule prefers delivering the answer. Watch the first live week of ledger rows for `done` plus a usage reading that jumped to a hard limit.
- **Shorter Grok timeout cuts long jobs.** A legitimate edit that needs more than `timeout_s` becomes `timed_out` and may be repeated on the next account (double spend). Set `timeout_s` from real job length, not only from failover impatience.
- **Lock skip spends another account.** If the lock wait is too short, a busy account looks failed and the next account runs the same packet. Bound the wait, and make the skip status visible on `attempts` so the bot can see `failed_retryable` from a lock versus from a CLI error.
- **Full replies in JSON.** A large packet result becomes a large stdout document. Callers that buffer the whole pipe must accept that. Redaction still runs on the whole text.
- **SSH `run_host`.** A mis-labeled host or a `reader_cwd` the CLI does not trust fails the attempt (trust prompt → retryable, or ssh failure → retryable) and walks. Readiness (`usage_layer.py read`) on that host before `route: true` is the mitigation. The router still will not log in.
- **Same bytes, one job id.** Two seats that submit the same packet file collide on the lease. Seats send distinct packet bytes when they want distinct jobs.
- **Import-time config.** Editing `route` under a long-lived process does nothing until restart. One-shot `live.py` avoids that. A future daemon has to re-read `FALLBACK` and `INVOKE` per call or it will dispatch to accounts that were turned off.

## What this branch does not do

No change to `dispatcher.py`, `live.py`, `harness_call.py`, `usage_layer.py`, or config. No throttle. No local model harness. No shared pool. No HTTP server. No merge of PR #1. No deploy. Live mode stays off unless an operator sets it outside this branch. The final plan in [hybrid scripted design](hybrid-scripted-design.md) is docs only on this same branch. It does not merge PR #1, deploy, or turn live on.
