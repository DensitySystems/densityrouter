# Hybrid scripted design — final plan

Plan only. Draft PR #2. Nothing here is merged, deployed, or turned live.

This revision answers three REVISE reviews (Critical Analyst, Network Architect, Cybersecurity Analyst) and the seat plan addendum of 2026-11-10 16:15 CST. The seat plan and the runtime receipt are not copied. Step 1, what public `main` does today, stays in [lb-activation-assessment.md](lb-activation-assessment.md).

The guarantee is **at-most-once per `job_id`**. It is not exactly-once. A timed-out CLI may still finish on its account until phase 1 lands. None of the three CLIs accepts an idempotency key.

## 1. Grok Build

Grok Build, in this plan, is one fixed program: `dr-front`. It is not an LLM seat, not `grok-build-cli.service`, and not the routed harness named `grok-build`.

Its role is to run the decision order in section 6 and then stop. It has no free-text arguments and no model step. Every decision point is a branch in that program.

Entry points, and no others:

| Call | Who invokes it | What it does |
|---|---|---|
| `route` | the intake, or a person on ws1 | dry run |
| `dispatch` | the intake, or a person on ws1 | claim, then a detached walk |
| `subscribe` | the same caller | output after `after_sequence` |
| `doctor` | a person on ws1 | one-shot report, no job |

The intake is the SSH forced-command in section 5. A local person uses the same binary. Grok Bot does not SSH to `live.py`.

The headless `grok agent` service and the `grok-build` harness share one CLI login. They do not share a lock today. Phase 3 puts both under the same per-account `flock`, or stops the headless service before LIVE. That choice is Jonathan's.

## 2. API

One JSON object on stdin. No second frame. No `packet_path` from the caller.

```json
{
  "schema": 1,
  "call": "dispatch",
  "job_id": "<16 hex from secrets.token_hex(8), never derived from the packet>",
  "session_id": null,
  "turn": 1,
  "intent": "start",
  "packet_b64": "<standard base64 of the packet bytes>",
  "packet_sha256": "<64 hex>",
  "after_sequence": 0,
  "until": "terminal",
  "max_wait_s": 900
}
```

Callers branch on `state`, not on the exit code.

`route` runs `dispatcher.py route-choose`. It does not claim a job and does not call a CLI. The response includes `would_send_to`, the rank fields, and, when a terminal receipt exists for that `job_id`, `dispatched_to` from the receipt. `would_send_to` is advisory: it is computed without the lease, the GO check, or LIVE. If a receipt exists, `route` still recomputes the ranking and does not overwrite the receipt.

`dispatch` with `intent: start` does the claim (section 3) and returns `{job_id, session_id, state: accepted}` as soon as the intent is stored and `systemd-run --user --unit=dr-<job_id>` has accepted the unit. It does not wait for `live.py`. Output arrives on `subscribe`.

Each interactive turn is its own `job_id`. The same `session_id` ties the turns. `turn` is an integer, starting at 1. The idempotency key is `job_id` alone. Turn 2's packet, which the front writes, begins with one fixed line naming the previous turn's `result_path`, then the new bytes. The front does not summarize. Until a long-lived process exists, the target is a new CLI process with that file as context. TUI continuity is deferred (section 9, CA-B3).

`session_id` is 32 hex chars (`secrets.token_hex(16)`), stored with the caller key fingerprint, the harness, `created`, and expiry (30 min idle, 4 h absolute). A later call must present the same fingerprint. A mismatch or an expiry is state `bad_session` and does not consume a new id. `intent: close` releases only a lock held by that same process.

`subscribe` prints audit rows with `seq` greater than `after_sequence`, then the redacted result file once the terminal row is in, after checking `result_sha256`. `until: terminal` exits on that row. `until: session_close` exits on `session_closed`. `after_sequence` greater than the last seq **at open** is state `bad_cursor`. A cursor that sits at the end and then waits is not `bad_cursor`. On `max_wait_s` the state is `subscribe_timeout` and the job id is not consumed. While waiting, if the claim lock is free, subscribe runs the same liveness test as the sweep.

`transport_down` is a caller-side state. SSH exit 255, or a failure of the tunnel client before a claim exists, uses this state. The same `job_id` is retried. It never consumes an id. It is not a router exit.

## 3. Claim, lock, and audit

Two mechanisms, both required:

1. **Claim.** stdlib `sqlite3` in WAL mode, file `state/router.db`, mode 0600. `jobs.job_id` is `PRIMARY KEY`. The insert is the atomic claim. The loser gets `in_flight` or the stored receipt. `events.seq` is `INTEGER PRIMARY KEY AUTOINCREMENT`.
2. **Lock.** The detached worker holds `fcntl.flock` on `state/jobs/<job_id>/claim` for the whole walk. A second flock, `LOCK_EX|LOCK_NB`, on `/run/densityrouter/acct-<uid>-<harness>.lock` (root-created, 0700, tmpfs, not under `DISPATCH_STATE`) covers that CLI login. The kernel drops both locks when the holder dies. There is no pid file. `close` does not release another process's lock.

`dp.sweep` writes `unknown_outcome` only when a non-blocking flock on the claim **succeeds** (the worker is gone) and the result file is missing or fails its sha check. If the flock fails, the state returned is `in_flight` and the id is not consumed. If the flock succeeds and `result.json` is complete, the sweep promotes that file to the terminal row instead of `unknown_outcome`.

The result file is written by the worker to `state/jobs/<job_id>/result.json` (tmp file, fsync, `os.replace`, directory fsync) **before** the terminal row is inserted. The receipt is that row plus that file.

`live.py` still computes its own id, `sha256(packet_sha + ":run")[:16]` (`dispatcher.py` lines 718–719). The audit stores that value as `router_job`. The worker also exports `DENSITYROUTER_JOB_ID` and writes the same id into the job directory, so a person can join `dispatch.log` to the audit. The CLIs do not read it.

Audit row fields, and no others. The serializer rejects any other key and any value over 256 bytes:

`seq`, `ts`, `prev`, `dp`, `action`, `state`, `exit`, `job_id`, `session_id`, `turn`, `call`, `packet_sha256`, `router_job`, `harness`, `result_path`, `result_sha256`, `caller_fpr`, `notify`, `consumes`

`prev` is the sha256 of the previous row's canonical bytes (`seq` 1 uses 64 zero hex digits). Append is one transaction that writes the row, then one journald record `SyslogIdentifier=densityrouter-audit` with the same bytes. Journald is the off-host copy, forwarded to the existing log host. The front checks the chain at startup. A break, a bad line, or a missing newline in the middle of a mirrored export is state `hold_audit_broken`, exit 9, notify Jonathan, and no dispatch.

`consumes: true` means a terminal row about that job: a finished walk, a redacted or refused body, `unknown_outcome`, or `secret_refused`. A hold that is about the machine (KILL, lock, offline, drift, missing GO file, unverified backends, transport) does not consume the id.

Window after the intent is stored and before the worker starts: if `systemd-run` fails and the unit is not active, the front deletes the claim and the id is not consumed. If the worker dies after the intent and before `live.py` starts, the sweep's `unknown_outcome` consumes the id and nothing ran. That is the at-most-once case.

Direct use of `live.py`, bypassing the front, is outside this claim. `lease_acquire` treats the same harness owner as re-entrant (`dispatcher.py` around the owner check near line 90). Two direct processes can both hold that lease. The front's claim is what covers the intake.

## 4. Central config

One file, the one `drconfig.py` already loads (lines 1–11 and 26–46): `densityrouter.config`, or `$DENSITYROUTER_CONFIG` for a non-intake run.

| Piece | Today | In this plan |
|---|---|---|
| Paths, harnesses, `lan`, `ssh`, `log_only_pools` | live tree, in use | unchanged. `invoke` stays the command list. |
| `capability-map.json`, `caps.json`, `usage-schema.json` | side files. Caps do not gate (`dispatcher.py` lines 651–654) | unchanged. Hashed by the manifest. |
| v02 `densityrouter.defaults.json` and `routing.profile` | on the v02 tree only. Not in this repo. Not on the live tree. Named profiles exist there. This plan does not copy their numbers | not deployed. Not imported. |
| `decision` | does not exist | operational bindings, section 4.1. The router code reads these. |
| `routing` | does not exist on the live tree | present, empty, schema-checked, **not read** by `route` or `live.py`. The slot is the mechanism. The rules stay unwritten. |

There is no second file and no `DISPATCH_DECISION_SCRIPT`. The intake ignores `DENSITYROUTER_CONFIG`, `DISPATCH_STATE`, `DISPATCH_KILL`, and `DISPATCH_IP_SCHEMA` from the environment and uses the root-owned paths in section 7.

### 4.1 `decision` (read by the front)

| Key | Default | Who may change it |
|---|---|---|
| `go_runs_gate` | `required` | a commit Jonathan merges. Not the panel. |
| `health.cooldown_s` | null (matches `dispatcher.py` line 583) | the same commit. Null means no skip. |
| `lock_wait_s` | 5 | the same commit. Expiry does not consume the id. |
| `backends_verified` | false | the same commit, after the live checks in phase 5. |
| `intake_daily_max` | absent | Jonathan sets the integer before the intake key exists. Absent means dispatch is refused. |
| `max_wait_s` | 900 | the same commit. Subscribe only. |

`rules` is not a free list. The only rebind the schema accepts is `panel_on_ws1` to `absent` (the default) or `loopback_readonly`. Everything else in the decision order is fixed code.

### 4.2 `routing` (not read)

Schema: an object that must be `{}` or absent. Any other content fails validation, exit 9, no dispatch. When Jonathan wants rules, a later change adds key names and a reader. That change is a yes of its own. The v02 profile names (density, clutch, and the others in the seat-plan addendum) are not copied in, and neither are their floors or breaker counts.

### 4.3 Validation and integrity

The front refuses, exit 9 `config_invalid`, unless all of the following hold:

- `densityrouter.config`, `capability-map.json`, and the manifest are owned by root, mode 0644, and `realpath` is under `/opt/densityrouter`.
- The database and `state/` are owned by user `router`, mode 0700.
- Unknown keys are rejected. `routing` is empty. `decision` matches the table.
- The manifest lists the sha256 of every `.py` the front imports, the front itself, `densityrouter.config`, and `capability-map.json`. It is built from the deployed tree in CI and installed root-owned. A CLI `--version` string is written on the intent row as a log line, not as a gate.
- Gate flags in section 4.1 change only by a commit. The panel cannot write them.

A detached signature over that manifest, with the private key kept off ws1, is part of the install Jonathan approves at deploy. Until that signature verifies, the front refuses dispatch. The public half ships in the root-owned install.

`paths.ip_schema` is a file on ws1, copied and refreshed by a person. The router never opens it over the network. A missing file keeps today's behavior: `lan_roles` is empty (`dispatcher.py` lines 477–492) and a Hosts line fails closed (lines 518 and 523).

## 5. Transport, panel, fallback

| Path | Decision |
|---|---|
| Caller | SSH forced-command to the existing ws1 SSH hostname. No new port. |
| Packet | `packet_b64` inside the one JSON object. The front decodes, rejects a caller-supplied path, writes `state/inbox/<sha256>.md` with `O_CREAT\|O_EXCL\|O_NOFOLLOW` mode 0600, hashes that fd, and passes that path. A sha mismatch is `hold_entry` and consumes nothing. `live.py` re-opens with `O_NOFOLLOW` and checks the sha again. |
| Key line | `restrict,no-user-rc,command="/opt/densityrouter/bin/dr-front",from="127.0.0.1,::1"` and one ed25519 key named by role and date. No key material in this doc. The file is a root-owned `AuthorizedKeysFile`, not a home-directory `authorized_keys`. |
| Command string | `dr-front` ignores `SSH_ORIGINAL_COMMAND`. A non-empty value is refused. The log records the length only. |
| Network gate | Cloudflare Access on that hostname, a policy for Jonathan, and a service token for the caller host. The key is a second factor, not the only gate. |
| Client | `ServerAliveInterval` on subscribe. A dropped channel after `accepted` loses nothing. |
| Panel | Bind `127.0.0.1:8941` on ws1 only, and only after the write routes are gone. Reach it with `ssh -L`. The user unit `densityrouter-panel-tunnel.service` is **disabled**, not repaired. No remote forward to any other host. |
| Fallback | One path, for Jonathan only: the registered-machine Shell on ws1, running the same `dr-front`. Not lan-mcp. |
| HTTP | No router HTTP. 8941 and the lan-mcp port stay loopback. A post-deploy `ss -lnt` check must show no new non-loopback listener. |
| Local socket | Not in this plan. |

Grok Bot's private key for the intake lives only on the caller host, mode 0600, and is not the interactive SSH key. Where on that host, and which agents can read it, is still unmeasured (section 11).

## 6. Decision order

KILL is first, matching `dispatcher.py` line 728. The root file `/etc/densityrouter/KILL` is checked, then the code path (`dispatcher.py` line 20 on public `main`; the live tree's default path is the one the seat addendum measured under the state dir). Either file stops the process, exit 3, state `killed`, before config is parsed. A missing router config, which today dies at import (`drconfig.py` lines 26–33), is caught and written as `config_invalid` when the front can still write; if it cannot, the process exits 9 with no audit row.

Order for every process: `dp.kill` → `dp.config` → `dp.audit` → `dp.drift` → `dp.entry` → the call.

`dp.secret` runs on `dispatch` only. `route` and `subscribe` do not consume an id because of packet text.

| id | Trigger | Action | State | Consumes |
|---|---|---|---|---|
| dp.kill | either KILL file exists | stop | `killed` | no |
| dp.config | config, owner, mode, or schema fails | stop | `config_invalid` | no |
| dp.audit | chain break or bad row | stop | `hold_audit_broken` | no |
| dp.drift | a hashed file differs, or the signature does not verify | stop | `hold_drift` | no |
| dp.entry | bad id, missing `packet_b64`, sha mismatch, path trick | stop | `hold_entry` | no |
| dp.secret | dispatch packet matches the redactor, or the redactor cannot run | stop | `redacted` | yes |
| dp.idempotency | terminal row exists | return the receipt | stored state | no |
| dp.inflight | claim exists and the flock is held | return | `in_flight` | no |
| dp.sweep | flock is free and the result file is incomplete | one terminal row | `unknown_outcome` | yes |
| dp.sweep | flock is free and the result file matches its sha | promote that file | the file's state | yes |
| dp.offline | LIVE is off, or a V3 window flag file `/etc/densityrouter/V3` exists | stop | `hold_offline` | no |
| dp.unverified | `backends_verified` is false | `route` still runs; dispatch stops | `hold_unverified` | no |
| dp.go_runs | gate `required` and the GO line or RUNS row is missing | stop. The front does not write either file | `hold_go_runs` | no |
| dp.go_runs | gate `off` while the deployed `dry_run` still requires the files | stop | `hold_go_runs` | no |
| dp.lock | account flock still held after `lock_wait_s` | stop | `lock_held` | no |
| dp.lease | `live.py` returns 5 | record it | `lease_held` | yes |
| dp.accept | claim inserted and `systemd-run --user` accepted the unit | return to the SSH client | `accepted` | no |
| dp.worker | the unit runs one `live.py`, no `--harness` on turn 1 | walk as the code walks today | the walk's state | yes when the terminal row is written |
| dp.turn | `session_id` set, fingerprint and expiry match | `--harness` is the stored harness only. Exit 7 closes the session | `pin_refused` or the walk's state | yes |
| dp.reply | result sha matches and status is `done` | subscribe prints the redacted file | `done` | yes |
| dp.reply | exact hash of a known secret matches the result or stderr | do not print the body | `secret_refused` | yes |
| dp.reply | stdout is not the expected JSON | store raw bytes only in a 0600 file under the job dir; do not echo them | `reply_unreadable` | yes |
| dp.quota | final status `failed_hold:usage` after the walk | record it. No throttle | `retry_exhausted` if nothing later returned `done` | yes |
| dp.timeout | final status `timed_out` | record it. Do not start a second walk | `retry_exhausted` if nothing later returned `done` | yes |
| dp.status | final `cancelled`, `REFUSED`, or `STALE` | stop. Those do not walk (`dispatcher.py` line 587) | that status | yes |
| dp.subscribe | `max_wait_s` elapsed | return | `subscribe_timeout` | no |
| dp.close | `intent: close`, fingerprint matches, this process holds the lock | release that lock | `session_closed` | yes |

`failed_hold:usage` and `failed_hold:not_installed` walk (`live.py` lines 5–6, `WALK_STATUSES` at `dispatcher.py` line 587). The seat plan's step 6 said they stop. This plan follows the code.

Notify Jonathan by one journald line and one row in the audit, for `killed`, `config_invalid`, `hold_audit_broken`, `hold_drift`, `unknown_outcome`, `secret_refused`, and `hold_secret_argv`. No second log file. No mail sender.

`doctor`, not a per-call branch, reports: panel unit state, caps file sha, whether `paths.ip_schema` exists, CLI version strings, and the argv scan. Those facts do not change a dispatch by themselves.

## 7. Process, credentials, redaction

Before LIVE, and before the intake key exists:

- User `router` runs `dr-front` and owns `state/`. User `cli-grok`, `cli-cursor`, and `cli-agy` run the three CLIs. The router user cannot write their homes. They cannot write `/opt/densityrouter` or `/etc/densityrouter`.
- Each CLI is started with a scrubbed environment (`env -i` and an allowlist: `PATH`, `HOME`, `LANG`, `DENSITYROUTER_JOB_ID`) and a writable path limited to that job directory.
- `dr-front` starts from `os.environ.clear()` plus the same allowlist.
- The Cursor worker API key is rotated first. The old value is treated as disclosed. The new value is never placed in argv, a unit `ExecStart`, an `invoke` template, a log, or a receipt. Delivery is systemd `LoadCredential` from `/etc/densityrouter/creds/cursor` (root, 0600), read from the credentials directory. The fallback is an `EnvironmentFile` on that worker unit only, same mode and owner. Not a shell profile.
- The worker runs as its own user. `/proc` is mounted `hidepid=2`.
- At startup, and again before each dispatch, the front scans readable `/proc/*/cmdline` for `--api-key`, `--token`, and `_KEY=`. A hit exits 9, state `hold_secret_argv`, and the audit line carries pid and `argv[0]` only.
- CI fails if a template, a unit file in the repo, or a test fixture contains `--api-key`, `api_key=`, `token=`, or the same prefixes. The test uses synthetic strings, never a real key.
- Prompt text is not put in argv where the CLI can read a file or stdin. Until that is verified per CLI (section 11), `hidepid=2` is what hides the argv that still exists.
- umask 077. Inbox, result, database, and job directories are 0600 or 0700.

Redaction fails closed. The front captures `live.py` stderr and runs it through the same redactor. A traceback is not forwarded: `live.run` is wrapped so any exception becomes exit 6, state `internal_error`, exception class only. An exact hash match against the credential store refuses the body and escalates. If the redactor itself fails, the body is not returned. The pattern list in `dispatcher.py` lines 46–57 is best-effort and is not the control. The controls are the user split, `hidepid`, and the exact-match refuse.

The panel, when it is imported, has its harness write routes and its usage-refresh route removed. A test asserts those paths return 404. A read of the audit log needs no secret. Any remaining config write requires a local auth secret from `LoadCredential` and the validator in section 4. That import is not required for LIVE.

## 8. What phase 1 must fix inside the walk

These land in the repo before `dispatch` is allowed to run on ws1. They are the Critical Analyst's sequencing point.

- Quota hints (`harness_call.py` lines 104 and 121–124) apply only when the attempt did not exit 0. A finished reply that contains those words stays `done`.
- The Grok session uses `timeout_s` and fences itself on timeout. A timeout kills the process group, not only the direct child.
- Until those are deployed, a timed-out attempt may still complete on its account. The at-most-once claim does not cover that duplicate.

`THROTTLE_PCT` stays `None` (`usage_layer.py` line 27). No local model harness.

## 9. Findings

| Reviewer | ID | Disposition | Where |
|---|---|---|---|
| Critical Analyst | B1 | adopted | section 3: sqlite claim, flock, sweep only if the lock is free, else `in_flight` |
| Critical Analyst | B2 | adopted | section 3: extra audit fields, result file replaced into place before the terminal row |
| Critical Analyst | B3 | adapted | section 2: one `job_id` per turn, prior `result_path` prepended. TUI continuity deferred: it needs a long-lived process, which section 5 refuses |
| Critical Analyst | B4 | adopted | section 5: `packet_b64`, `O_EXCL`, sha of the written fd |
| Critical Analyst | M1 | adopted | section 3: sqlite WAL plus flock. Guarantee renamed in the header. Result file closes the "work done, row missing" window |
| Critical Analyst | M2 | adopted | section 8, and phase 1 before any ws1 dispatch |
| Critical Analyst | M3 | adopted | section 2: `accepted` plus `systemd-run --user`. Subscribe carries the body |
| Critical Analyst | M4 | adopted | section 2: result sha, `max_wait_s`, `bad_cursor` only at open |
| Critical Analyst | M5 | adopted | section 4: one file, empty `routing`, v02 profiles left out |
| Critical Analyst | M6 | adapted | section 5 and 7: CLI stays the interface that works. Panel is read-only and loopback, write routes removed, import not required for LIVE |
| Critical Analyst | M7 | adopted | section 1 |
| Critical Analyst | M8 | adopted | section 6: KILL first, secret on dispatch only, branch on `state`, rows for missing config and a bad audit |
| Critical Analyst | M9 | adopted | section 4.3: every imported `.py`, the front, the config, the capability map. CLI versions are a log line |
| Critical Analyst | m1 | adopted | section 6: `doctor`. The exposed-key boolean is gone |
| Critical Analyst | m2 | adopted | section 2: fresh rank plus `dispatched_to` |
| Critical Analyst | m3 | adopted | section 3: flock held by the worker, `lock_held` is its own state |
| Critical Analyst | m4 | adopted | phase 1 test for Hosts lines with an empty role set |
| Critical Analyst | m5 | adopted | section 3, last paragraph: the lease hole remains for direct `live.py` |
| Critical Analyst | m6 | adopted | section 2: `token_hex(8)`, not a hash of the packet |
| Critical Analyst | m7 | adopted | sqlite is the store. A JSONL file is not |
| Critical Analyst | m8 | adopted | caller-versus-pool is a gate in section 10, not a closed question. Relay does not count as Jonathan's own yes |
| Critical Analyst | m9 | adopted | section 12 |
| Critical Analyst | m10 | adopted | section 5: `restrict,no-user-rc,command=`, plus `from=` loopback so the key is dead on the LAN |
| Network Architect | B1 | adopted | section 5: Access, service token, `restrict`, `from=` loopback, one dedicated key, `SSH_ORIGINAL_COMMAND` refused |
| Network Architect | B2 | adopted | section 2 and phase 2 test: kill the SSH client after `accepted`; the unit still writes the terminal row |
| Network Architect | M1 | adopted | section 5: disable the reverse-tunnel unit. Panel is loopback plus `ssh -L` |
| Network Architect | M2 | adapted | section 10 phase 3: ws1 address persistence is a network task. The tunnel unit is disabled rather than re-measured as a repair. The crash-loop stops because the unit is disabled |
| Network Architect | M3 | adopted | section 2 `transport_down`; section 6 `dp.offline` for a V3 flag file; section 5 fallback |
| Network Architect | m1 | adopted | phase 1 test: route and dispatch open no lab socket |
| Network Architect | m2 | adopted | section 4.3: schema file is local |
| Network Architect | m3 | adopted | section 5 and the post-deploy `ss` check |
| Network Architect | m4 | adopted | section 3 and 6: one walk at a time; `lock_held` does not consume |
| Network Architect | m5 | adopted | section 10: key rotation before the intake key and before LIVE |
| Cybersecurity Analyst | B1 | adopted | section 7. The boundary does not exist until phase 3. LIVE waits on that phase |
| Cybersecurity Analyst | B2 | adopted | section 5. Key possession as approval is a written yes in section 10 |
| Cybersecurity Analyst | B3 | adopted | section 7. Rotation before LIVE. Startup scan replaces the boolean |
| Cybersecurity Analyst | M1 | adopted | section 3 per-account flock outside `DISPATCH_STATE`. Shared grok login stated in section 1 |
| Cybersecurity Analyst | M2 | adopted | section 4.3 |
| Cybersecurity Analyst | M3 | adopted | section 3: `prev`, flock not required for sqlite's transaction, journald mirror, startup chain check. The 256-byte value cap is in the field list |
| Cybersecurity Analyst | M4 | adopted | section 7: fail closed, stderr captured, class-only errors |
| Cybersecurity Analyst | M5 | adopted | section 2: 128-bit id, fingerprint, expiry |
| Cybersecurity Analyst | M6 | adopted | section 5 and 7: write routes deleted, auth if a write remains, tunnel disabled |
| Cybersecurity Analyst | M7 | adopted | section 5 framing |
| Cybersecurity Analyst | m1 | adapted | section 7: file or stdin where the CLI allows it. `hidepid=2` covers argv that remains. Which CLIs allow it is unmeasured |
| Cybersecurity Analyst | m2 | adopted | section 7: cleared environment |
| Cybersecurity Analyst | m3 | adopted | section 6: `/etc/densityrouter/KILL` first |
| Cybersecurity Analyst | m4 | adopted | section 7: umask 077 |
| Cybersecurity Analyst | m5 | adopted | section 4.1: no security rebind. The boolean is gone |
| Cybersecurity Analyst | m6 | adopted | section 6 `dp.reply` quarantine |
| Cybersecurity Analyst | m7 | adopted | `caller_fpr` on every audit row |

Nothing in the three reviews is left without a row.

## 10. Phases and gates

A draft in the repo is not a yes. Merge, deploy, LIVE, a live model call, a network change, a Cloudflare change, a key, and a host change are Jonathan's own yes. A relayed note is not that yes.

| Phase | Work | Gate |
|---|---|---|
| 0 | Ruling: Grok Bot is a caller, and the `grok-bot` harness stays out of the pool. If the answer is that it becomes a pool, this plan stops. Written ruling: after the GO gate is gone, possession of the intake key is the approval. Set `intake_daily_max`. | **Jonathan: ruling, in writing** |
| 1 | In repo, before any front dispatch: section 8 (quota, Grok timeout, process group), fail-closed redaction, stderr capture, class-only errors, `O_NOFOLLOW` sha recheck, Hosts-line test, no-lab-socket test, CI secret grep. | Draft is ordinary review. **Jonathan: merge to `main`** |
| 2 | In repo: `dr-front`, sqlite claim, flocks, `packet_b64`, detached `systemd-run --user`, subscribe, hash-chained audit, manifest check, decision order. Test: kill the client after `accepted` and still get one terminal row. | Draft is ordinary review. **Jonathan: merge to `main`** |
| 3 | On ws1, before LIVE and before the intake key: user split, scrubbed CLI env, `hidepid=2`, `LoadCredential`, root-owned `/opt/densityrouter` and `/etc/densityrouter/KILL`, rotate the Cursor worker API key, disable `densityrouter-panel-tunnel.service`, persist ws1's lab address, Cloudflare Access plus the service token, install the forced-command line. | **Jonathan: host change, secret, service change, network, Cloudflare.** Rotation is before LIVE and before the intake key |
| 4 | Deploy one pinned tree to a new directory, manifest from that tree, local `ip_schema` copy, state carried only by a separate decision, new tree **LIVE off**, `doctor`, `ss -lnt` shows no new non-loopback listener. | **Jonathan: deploy and router-config write** |
| 5 | L0 and L1 reads (bin `--version`, `usage_layer.py read`). Then one pinned live call per backend. Then LIVE on. Then the seat plan's end-to-end checks. | **Jonathan: live calls, then the live switch** |
| 6 | Panel sources imported read-only, write routes already deleted, loopback plus `ssh -L` only. | **Jonathan: router deploy.** Not required for phase 5 |

Phase 3's address persistence is a network task. It is not a densityrouter code change. The named tunnel is outbound from ws1, so a later address change does not by itself move the SSH hostname. LIVE stays off for any window that changes ws1's port, VLAN, or address (`/etc/densityrouter/V3`).

The seat plan's symlink swap, rollback (`live.py --off` removes the LIVE file; it does not unset `DISPATCH_LIVE`), and T1–T4 remain that plan's runbook. Two corrections stand: KILL is not created only under the state dir (section 6), and a usage hold walks (section 6).

## 11. Unmeasured, not guessed

These stay open because the reviews named them and this plan did not measure them:

- OpenSSH version on ws1, and the current `PermitUserEnvironment`, `AcceptEnv`, `ExposeAuthInfo`, and `AuthorizedKeysFile`.
- Whether `/proc` already uses `hidepid`.
- Who holds the Cursor worker unit, and whether `cursor-agent` on the router uses the same credential.
- Whether `cursor-agent -p --trust` and `grok -p` on the installed versions can run a shell.
- Whether the router user can sudo.
- Which processes on the caller host will be able to read the intake private key.
- The Cursor key's text shape. The CI grep uses synthetic prefixes only.

## 12. Residual risks

These are expected. No new gate is added for them.

- ws1 is one host. Load balancing is across CLIs on that host. If ws1 or the tunnel is down, the caller sees `transport_down` and retries the same id. There is no second router.
- One intake key. Loss or theft is a full dispatch right until the key line is removed and the Access token is revoked.
- The audit database is local. The journald mirror is the copy the router user cannot edit after it has left the box. A mirror that is down does not by itself block dispatch; a chain break on the local database does.
- ws1 power loss can still leave a claim with no result. The sweep then consumes the id as `unknown_outcome` if the worker is gone. That is at-most-once, not a silent second run.
- The headless grok service and the `grok-build` harness share a login until phase 3 separates them or stops one.
- Redaction patterns do not catch a secret that was split or re-encoded. The refuse path catches an exact hash. Anything else can still leave the box inside a model reply. The user split is what limits how the model got the secret.
- v02's panel, if imported without the deletions in section 7, writes config. Phase 6 is the only import path, and it is after those deletions.
- Public `main` and the tree on ws1 are different commits. `dp.drift` refuses dispatch until the manifest matches the tree that was actually installed.
