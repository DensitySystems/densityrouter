# Hybrid scripted design (candidate)

Candidate for a critical-analysis review. Plan only. Nothing here is merged, deployed, or turned live.

This file does not repeat the seat plan `PLAN-DENSITYROUTER-LB-ACTIVATION-20261110` or the runtime receipt `DENSITYROUTER-LB-ACTIVATION-RUNTIME-20261110`. Those stay the evidence for what ws1 is doing. This file is the repo-side candidate: the API, the decision script, and the places the seat plan and this repo disagree. Step 1, what the code does today, stays in [lb-activation-assessment.md](lb-activation-assessment.md).

Direction this candidate is built to:

- Grok Build is the front. It executes a script. It does not choose a harness on the fly.
- Grok Bot is a caller. The harness key `grok-bot` stays `route: false`, with no invoke path and no pool. The seat plan left that as an open ruling. The later direction (Grok Bot asks where a job would go, and may also send a job) selects caller, not pool. Putting `grok-bot` into the ranked set still needs a separate yes. This candidate does not do it.
- No throttle. No local model tier. No new routing formula. `THROTTLE_PCT` stays `None`.

## 1. What already exists in this repo

### 1.1 Central config

There is one config system. `drconfig.py` calls it the one place for paths, hosts, and harnesses.

| What | Where | What it does today |
|---|---|---|
| Loader | `drconfig.py` lines 1–11, 26–33, 40–46 | `$DENSITYROUTER_CONFIG`, else `densityrouter.config` beside the code. Missing file exits 9. The `.example` is never loaded silently. Keys whose names start with `_` are comments. Cache key is path, mtime, size. |
| Harness list | `drconfig.py` lines 59–67 | `harnesses()` keeps file order. `routed()` is `route: true` only. That list is the fallback order. |
| Path and env overrides | `drconfig.py` lines 54–57 and 10–11 | `paths.<key>` from the file. `DISPATCH_STATE`, `DISPATCH_IP_SCHEMA`, `DISPATCH_USAGE_DIR`, `USAGE_LEDGER`, `GROK_BUILD_BIN`, `CURSOR_AGENT_BIN` win for one process when set. |
| Template | `densityrouter.config.example` | Ships the key names. Real values stay in the gitignored config. |
| Ability data | `capability-map.json` | Numbers per harness per task type. Not a rules file. `dispatcher.route` reads them as A. |
| Cap labels | `caps.json` | `enabled`, `used_today`, caps. `dispatcher.py` lines 30–31 and the route comment at lines 651–654: `enabled` is not a balancer flag and caps are not a skip. |
| Live, kill, state | `dispatcher.py` line 20; `live.py` lines 17–26 | `DISPATCH_LIVE=1` or `<state-dir>/LIVE`. KILL is `$DISPATCH_KILL` or `<code-dir>/KILL` (line 20), not a file under the state dir. |
| Cooldown and throttle | `dispatcher.py` line 583; `usage_layer.py` line 27 | Both constants are `None`. Nothing in the config file sets them. |
| Imported once | `dispatcher.py` line 23; `usage_layer.py` lines 30–33 | `FALLBACK`, pools, and parsers are fixed at import. A long-lived process keeps them until restart. |

The seat plan's deploy and config-carry steps stay in that plan. This candidate adds one file the loader does not know yet: `decision-script.json`, pointed at by a new `paths.decision_script` key and overridden by `DISPATCH_DECISION_SCRIPT`. It is not a second config system. `drconfig.load` remains the loader. The new file holds decision bindings only. The validator rejects score weights, task-type keywords, `route` flips, cap numbers, a throttle, and a local-model harness. Routing rules stay unwritten. Jonathan's later edits are data in that file, checked against a closed list.

### 1.2 Human interface

This repo has no panel module, no HTTP server, and no listening socket. A search of the Python sources finds CLIs only (`dispatcher.py` line 729, `live.py` line 274, `usage_layer.py` line 718, `usage_writer.py` line 333).

| Surface | Where | What a person gets today |
|---|---|---|
| Rank, no call | `dispatcher.py` lines 732 and 747–752 | `route-choose <packet>` prints the route JSON. It does not read the GO ledger. If every usage snapshot is missing it also prints `PHASE1_NOTE` and a precedence line before the JSON. |
| Dry run | `dispatcher.py` lines 733–734 and 753–757 | `dispatch --dry-run` runs `dry_run` (lines 712–723). On this `main` that requires a GO line and a RUNS row. Exit 0 or 2. It never calls a CLI. |
| Live call | `live.py` lines 268–281; `dispatcher.py` lines 736–761 | `live.py <packet>` or `dispatcher.py live`. JSON on stdout, exit code on the process. `--harness` pins and disables the walk. |
| Live off | `live.py` lines 28–42 and 270–272 | `--off` removes `<state-dir>/LIVE`. It does not unset `DISPATCH_LIVE`. |
| Who owns a harness | `dispatcher.py` lines 30–31 and 762–763 | `adapters` prints owner and off-switch labels, and the KILL path. |
| One usage screen | `usage_layer.py` lines 720–726 | `read <harness>` types `/usage` and does not send a model prompt. `run` calls one named harness and skips the walk. `rollup` prints totals. |
| Snapshots | `usage_writer.py` lines 333–347 | `record` and `snapshot` write the usage files the ranker reads. |
| Call log | `dispatcher.py` lines 219–220 | One `dispatch.log` line per live attempt: job, harness, start, end, before, after. |
| Docs | `README.md`, `USAGE.md` | The operator text for the same commands. |

The panel a person would open is not in this repo. The seat plan measured one user unit, `densityrouter-panel-tunnel.service`, forwarding port 8941 to core-a, crash-looping since 2026-10-31, and no `panel.py` process. The v02 tree on ws1 holds `panel.py`, `panel.html`, and `densityrouter.defaults.json`, and it is not the live tree. The seat plan's step 2 recommends leaving that panel out of activation.

This candidate does not follow that recommendation, and it does not replace the panel. Routing and submit do not wait on the tunnel (the seat plan is right that the tunnel blocks the panel, not the CLI). The human interface to keep is that unit plus those v02 files. Extending them means: they read the same audit log as the CLI, and a validated save of `decision-script.json` is the only edit they offer. No free-text harness picker. Bringing the v02 files into the repo is the seat plan's drift reconcile, with the panel included instead of dropped. Until those files are in the repo there is nothing here to patch. G6 (repair or disable the unit) stays a yes. This candidate does not restart the unit and does not bind a new port.

`route-choose` is the advisory call Grok Bot can make today. `live.py` is the send. Neither one streams.

## 2. T3 Code

Read for this candidate:

- Product page: [https://t3.codes/](https://t3.codes/)
- Repository: [https://github.com/pingdotgg/t3code](https://github.com/pingdotgg/t3code)
- Architecture: [https://github.com/pingdotgg/t3code/blob/main/docs/internals/overview.md](https://github.com/pingdotgg/t3code/blob/main/docs/internals/overview.md)
- Command idempotency: [https://github.com/pingdotgg/t3code/blob/main/docs/orchestration-v2/README.md](https://github.com/pingdotgg/t3code/blob/main/docs/orchestration-v2/README.md)
- Session and event model: [https://github.com/pingdotgg/t3code/blob/main/docs/orchestration-v2/core-graph-and-data-model.md](https://github.com/pingdotgg/t3code/blob/main/docs/orchestration-v2/core-graph-and-data-model.md)
- Subscribe cursor: `OrchestrationSubscribeThreadInput` in [packages/contracts/src/orchestration.ts](https://github.com/pingdotgg/t3code/blob/main/packages/contracts/src/orchestration.ts) (`afterSequence` replays, then live events; the client dedupes by sequence)

Borrowed, and only this:

1. Execution stays on the machine that owns the workspace and the CLI logins. A remote caller does not bring its own filesystem or credentials. From `docs/internals/overview.md`: the remote client must not substitute its own filesystem, provider credentials, or machine state. Here that machine is ws1. Grok Bot does not run the CLIs.
2. A dispatch commits an intent, then side effects run, then subscribers see the log. From the same overview: the orchestrator decides events without doing provider I/O; the commit of the event, the receipt, and the outbox is one transaction; subscribers are notified after that commit; an acknowledgement means the intent was stored, not that the provider finished.
3. The same command id returns the stored receipt and does not run the provider again. From `docs/orchestration-v2/README.md`: duplicate command ids return the same receipt-backed result without re-running provider side effects.
4. `subscribe` takes `after_sequence` and replays, then follows. From the contract: when `afterSequence` is set, the server skips the snapshot and replays events after that sequence before live events.
5. A session handle is durable metadata (which harness, which cwd, status), not the live process. From `core-graph-and-data-model.md`: `ProviderSession` is durable metadata; the process can die and be recreated; raw provider frames are not the durable event. Here the durable event is the redacted status row. The TUI screen is not the audit log.

Not borrowed: the Node server, WebSocket RPC, Effect, Electron and mobile clients, git checkpoints, mid-thread model switching, Tailscale, a token broker, and a database. This repo is stdlib Python. A JSONL file is not their transaction. Section 12 states that gap.

Their product page describes one surface over Claude Code, Codex, Cursor, Grok, and others, using each tool's own subscription. That matches "each CLI, own account" already. It is not a design we copy past the five items above.

## 3. Shape

```
Grok Bot
  hands one job JSON to Grok Build
Grok Build
  execs the front script and does nothing else with the router
front script
  reads decision-script.json
  route     -> dispatcher.py route-choose     (no live.py)
  dispatch  -> one live.py, no --harness       (pin only on a later turn)
  subscribe -> reads the audit log
live.py
  ranks, walks, returns JSON
audit log
  one append-only JSON object per decision
panel (when the forwarder is actually up)
  reads that log; does not call a CLI itself
```

Grok Build's rule, until the front exists and after: do not invoke `live.py` or `usage_layer.py run` itself. If the front is missing, stop and record that for Jonathan. Do not improvise a harness.

The seat plan's SSH forced-command is the recommended way Grok Bot reaches that front. See section 6. The forced command runs the front, not a raw `live.py`, so `route` and `subscribe` exist on the same auth path as `dispatch`.

## 4. Candidate API

One job object. Three calls. No fourth verb.

```json
{
  "schema": 1,
  "job_id": "0123456789abcdef",
  "packet_sha256": "<64 hex of the packet bytes>",
  "packet_path": "<path under the router state dir>",
  "call": "route",
  "intent": "start",
  "session_id": null,
  "after_sequence": null,
  "until": null
}
```

`call` is `route`, `dispatch`, or `subscribe`. `intent` is `start` or `close` and is read only by `dispatch`. `until` is `terminal` or `session_close` and is read only by `subscribe`. A missing field that the call requires is a refusal, not a guess.

`route` maps to `route-choose` (`dispatcher.py` lines 747–752). It returns `would_send_to` (first ranked harness, or null), `decided_by`, `task_type`, `ranked` (harness, A, U, H, score, usage), `skipped`, `health_gap`, and `basis`. It does not take a lease and does not call a CLI. It may be repeated. Each call appends an audit row. It does not occupy the job's dispatch receipt. Allowed stdout before the JSON is only the two strings `route-choose` already prints (`PHASE1_NOTE`, and the precedence line). Any other prefix is `reply_unreadable`.

`dispatch` with `intent: start` and no `session_id` runs `live.py` once, without `--harness`. The response is the job id, a new session handle `{session_id, harness, status}`, the router exit, and the path of the result file. The harness on the handle is `call.harness` after the walk, which can differ from `would_send_to`.

`dispatch` with `session_id` set is a later turn. The script passes `--harness` of the harness stored on that session and no other. Exit 7 closes the session and does not pick a replacement.

`dispatch` with `intent: close` releases the harness lock and appends `session_closed`. It does not call a CLI.

`subscribe` prints audit objects for that job with `seq` greater than `after_sequence` (zero means from the start). `until: terminal` exits after the terminal event (broadcast). `until: session_close` keeps reading until `session_closed` (interactive). The panel and a dropped caller both use this. They do not open a second channel.

Today `live.py` prints one JSON document at the end (`live.py` lines 280–281). A subscribe that starts before dispatch returns sees the intent row, then silence, then a burst of attempt rows the front writes from that JSON. Chunk streaming of model tokens is not available from these CLIs. The cursor is still the right API, so a later harness that emits lines can append without a new call.

## 5. Idempotency and the audit log

`dry_run` today sets the job id to the first 16 hex chars of SHA-256 of `packet-sha256 + ":run"` (`dispatcher.py` lines 718–719). The same bytes are the same job. After the lease is released the same bytes can be sent again.

This candidate splits those. `packet_sha256` stays on the receipt. `job_id` is 16 hex chars supplied by the caller (Grok Build mints it if the caller did not). Dispatch side effects for one `job_id` run at most once.

The audit log is `<state-dir>/audit.jsonl`. Append only. One JSON object per line. `seq` is a decimal integer, plus one each line. Fields, and no others:

`seq`, `ts`, `dp`, `action`, `state`, `exit`, `job_id`, `call`, `notify`, `consumes`

`notify` is `none`, `caller`, `jonathan`, or `panel`. `caller` means the field is in the JSON the front prints. `jonathan` means the same line is also appended to `<state-dir>/escalate.jsonl` with no packet body. `panel` means the panel reads `audit.jsonl`. There is no mail or chat sender.

A dispatch whose receipt already exists returns that receipt and appends `dp.idempotency` with `consumes: false`. It does not call `live.py`.

`consumes: true` is written only for a terminal outcome about that job (a finished walk, a redacted packet, a bad packet, an unknown outcome after a crash). Lock, KILL, a missing config, code drift, a missing GO line, and "backends not verified" do not consume the id. The same id can be presented again after the condition is gone. That is the seat plan's "rc 2/5/7/8 are safe to resubmit", narrowed so a finished walk is not resubmitted under the same id.

Crash rule, because a file is not T3's transaction: the front appends and fsyncs an `intent` row before `live.py`, and a terminal row after. On the next start, an `intent` with no terminal becomes `unknown_outcome`, `consumes: true`, notify Jonathan. The harness is not called again. A person who wants another try mints a new `job_id`.

## 6. Transport

| Option | What it is | Cost | Candidate |
|---|---|---|---|
| CLI | `python3` front with `route`, `dispatch`, `subscribe` on ws1. Stdin or a packet path. JSON and JSONL on stdout. | No port. Subscribe is a process that reads the log. A live Grok TUI dies when `live.py` exits, which is already true (`usage_layer.grok_router_session` is one process). | Use this. |
| SSH forced-command | The seat plan's intake: one key whose command is only the front, with no pty and no forwarding. Grok Bot sends the job JSON on stdin. | No new port. The key line is an access change and needs a yes. This doc does not copy the command or the host. | Use this as the caller path, once the front exists. The forced command is the front's three calls, not raw `live.py`. |
| Local socket | A long-lived process on ws1, filesystem socket, same three calls. | Holds a TUI across turns. A supervisor is new. Still no network. | Not in the first build. Open question 4. |
| HTTP on ws1 | Something bound on 8941, which is the port the panel forwarder already uses. | A listener is a network change. Nothing listens there today. Auth for HTTP does not exist; the CLI's trust boundary is the OS user. | Do not add one. The panel, when it is running, is the only program on that port, and it serves the existing UI plus a read of the audit log. |

`usage_layer.py run` stays a manual one-harness tool. The front does not call it.

## 7. Decision script

The front loads `decision-script.json` first. Invalid file: exit 9, state `config_invalid`, no call. These ids cannot be rebound: `dp.config`, `dp.kill`, `dp.secret`, `dp.drift`, `dp.entry`, `dp.idempotency`, `dp.sweep`. A `rules` entry may rebind only an id marked overridable, and only to an action listed on that row. Unknown keys are invalid.

Defaults the validator accepts (routing keys are absent on purpose):

- `schema`: 1
- `go_runs_gate`: `required` (this `main` and ws1). `off` only after PR #1 is merged and the deployed tree is that commit.
- `health.cooldown_s`: null
- `lock_wait_s`: 5
- `backends_verified`: false
- `cursor_key_exposed`: true until Jonathan rotates the Cursor worker API key. The front reads the boolean. It does not read process arguments and it does not log a key.
- `code_manifest`: sha256 of `dispatcher.py`, `live.py`, `harness_call.py`, `usage_layer.py` for the pinned commit
- `panel.down_action`: `proceed_without_panel`
- `rules`: `[]`

```
START
  dp.config / dp.drift / dp.kill / dp.entry / dp.secret
  call = route      -> dp.route -> return
  call = subscribe  -> dp.subscribe -> return
  call = dispatch
      receipt exists -> dp.idempotency -> return
      intent without terminal -> dp.sweep -> return
      dp.offline / dp.unverified / dp.go_runs / dp.lock
      dp.select (one live.py)
      dp.reply
      terminal audit row
```

`dp.panel`, `dp.caps`, `dp.schema_missing`, and `dp.cursor_key` run beside this. They do not choose a harness.

Log line, one JSON object, no packet text:

`{"seq":1,"ts":"<iso seconds>","dp":"dp.route","action":"route_choose","state":"advisory","exit":0,"job_id":"<id>","call":"route","notify":"caller","consumes":false}`

| id | Trigger | Action | Exit / state | Notify | Consumes | Overridable |
|---|---|---|---|---|---|---|
| dp.entry | `call` not in the three, bad hex id, empty packet, packet path outside the state dir | refuse | 2 `hold_entry` | caller | no | no |
| dp.secret | packet matches `dispatcher.redact_text` | refuse, do not call | 8 `redacted` | caller | yes | no |
| dp.kill | KILL file from `dispatcher.py` line 20 exists | refuse | 3 `killed` | caller, jonathan | no | no |
| dp.config | decision script missing or invalid | refuse | 9 `config_invalid` | jonathan | no | no |
| dp.drift | file sha256 != `code_manifest` | refuse route and dispatch | 9 `hold_drift` | jonathan | no | no |
| dp.route | `call` is route, KILL clear | `route-choose`. Empty rank: 2 `hold_unranked`. Else 0 `advisory` | 0 or 2 | caller | no | no |
| dp.subscribe | `call` is subscribe | print rows with `seq` > `after_sequence`. Bad cursor: 2 `bad_cursor` | 0 or 2 | caller | no | no |
| dp.idempotency | dispatch and a terminal row exists for this id | return the stored receipt | stored exit | caller | no | no |
| dp.sweep | `intent` row and no terminal row | append `unknown_outcome`. Do not call | 6 `unknown_outcome` | jonathan | yes | no |
| dp.offline | dispatch and live is off (`live.py` lines 24–26) | refuse | 2 `hold_offline` | caller | no | no |
| dp.unverified | dispatch and `backends_verified` is false | refuse. `route` still runs | 2 `hold_unverified` | jonathan | no | no |
| dp.go_runs | gate `required`, and `find_go` / `find_runs` miss (`dispatcher.py` lines 716–720) | refuse. Do not write a GO line | 2 `hold_go_runs` | caller, jonathan | no | no |
| dp.go_runs | gate `off`, and the deployed `dry_run` still requires the files | refuse dispatch | 2 `hold_go_runs` | jonathan | no | no |
| dp.go_runs | gate `off`, and PR #1's `dry_run` is what is deployed (`ok` is "ranked is non-empty") | skip this check | pass | none | no | no |
| dp.lock | `state/locks/router` still held after `lock_wait_s` | do not start `live.py` | 5 `lock_held` | caller | no | no |
| dp.lease | `live.py` exits 5 | return it. Do not retry | 5 `lease_held` | caller | no | no |
| dp.select | first start, checks passed | one `live.py`, no `--harness` | router exit | caller | yes if the process returns | no |
| dp.select | start with `session_id` | one `live.py --harness` of the stored harness only | router exit, or 7 `pin_refused` which closes the session | caller | yes | no |
| dp.timeout | final status `timed_out` (`harness_call.exit_map`, line 122). Grok's session wait is 600 s and ignores `timeout_s` | return the walk result. Do not start a second `live.py` | 6 `retry_exhausted` if nothing later returned `done` | caller | yes | no |
| dp.quota | final status `failed_hold:usage` (hints at `harness_call.py` line 104, applied to the reply text at line 123) | same: the walk already happened. Do not throttle | 6 `retry_exhausted` if it is the final status | caller | yes | no |
| dp.cooldown | `health.cooldown_s` is null (`dispatcher.py` line 583) | do not skip. A failure row does not change the next ranking | pass | none | no | yes, the number only |
| dp.cooldown | `health.cooldown_s` is a positive integer | once `route` reads it, `health_skip` drops a failure younger than N. `done` overwrites the row | pass | none | no | yes, the number only |
| dp.reply | stdout is not the expected JSON | do not retry | 6 `reply_unreadable` | caller, jonathan | yes | no |
| dp.reply | `done` and stdout length is 4000 (Grok session slice) or 2000 (`harness_call` slice) | return the text | 0 `done_truncated` | caller | yes | yes: `return` or `refuse` |
| dp.reply | `done` and `redaction_hits` > 0 | return the masked text. Do not resubmit | 0 `done_redacted` | caller | yes | no |
| dp.reply | `done` and empty stdout | do not retry | 6 `reply_empty` | caller | yes | no |
| dp.status | final `done` | return it | 0 `done` | caller | yes | no |
| dp.status | final `PENDING` | `subscribe` until the receipt or the pending deadline, poll every 30 s. Shipped harnesses are `async: false` | 0 while pending, 6 `stale` at the deadline | caller | yes | no |
| dp.status | final `cancelled`, `REFUSED`, or `STALE` | stop. `cancelled` does not walk (`WALK_STATUSES`, line 587) | 6 and that status | caller; jonathan if `cancelled` or `REFUSED` | yes | no |
| dp.status | final walk status (`failed_retryable`, `timed_out`, `failed_hold:usage`, `failed_hold:not_installed`, unpinned `NOT_DISPATCHED`) after every candidate | stop. Those statuses do walk (`live.py` lines 5–6) | 6 `retry_exhausted` | caller | yes | no |
| dp.panel | forwarder unit not staying up, or nothing on 8941 | `proceed_without_panel`: CLI result still returns. One escalate line per outage | the dispatch's own exit | jonathan once | no | yes: `proceed_without_panel` or `refuse` |
| dp.schema_missing | `paths.ip_schema` missing. `lan_roles` returns empty (`dispatcher.py` lines 477–492). A Hosts line then fails closed (lines 518 and 523) | log and continue. Do not invent role names | pass | jonathan once | no | yes: `log_continue` or `refuse` |
| dp.caps | caps file sha differs from the manifest. Caps do not gate (route comment, lines 651–654) | log and continue | pass | jonathan once | no | yes: `log_continue` or `refuse` |
| dp.cursor_key | `cursor_key_exposed` is true | one escalate line per outage. Do not print a secret. Do not change ranking | pass | jonathan once | no | yes: `notify_continue` or `refuse` |
| dp.session_close | `intent: close` | release `state/locks/<harness>`, append `session_closed` | 0 `session_closed` | caller | yes | no |
| dp.escalate | any row whose notify includes jonathan | the row already wrote `escalate.jsonl`. Do not write a second line in the same run | the row's exit | jonathan | the row's flag | no |

Harness selection is `dp.route` and `dp.select`. The script does not sort, and it does not pass `--harness` on a first start. Interactive follow-up is the stored harness only.

The seat plan's step 6 says a usage hold or a missing binary stops the walk. That does not match `WALK_STATUSES` (line 587) or `live.py` lines 5–6. This candidate follows the code: those two statuses walk; `cancelled`, `REFUSED`, and `STALE` stop; exit 6 is the finished failure, not exit 2.

The seat plan's rollback says a KILL file under the state dir. `check_kill` (`dispatcher.py` lines 33–35) reads line 20's path. A state-dir KILL file does nothing unless `DISPATCH_KILL` points at it. The emergency stop stays `touch <code-dir>/KILL` or `live.py --off` for the LIVE file.

## 8. G1–G9

| Gap | Where it lives here | Yes still required |
|---|---|---|
| G1 merge PR #1, or new packets stay at exit 2 | `dp.go_runs`. This candidate does not write GO lines and does not merge. | Yes, to merge or to deploy the PR head. |
| G2 redeploy ws1 from one commit; reconcile `usage_layer.py` drift, `meter_ledger.py`, and the v02 tree | `dp.drift` plus the seat plan's step 2 and step 4. Include the panel files in that reconcile. | Yes, to merge and to deploy. A draft diff is not a yes. |
| G3 an intake that is not a hand-written file | Section 6. The forced-command front is the intake. Writing packet bytes into `state/inbox` is that command's job. | Yes, for the key line. No, for a new port. |
| G4 caller or pool | Section 3. Caller. `grok-bot` stays out. | The later direction selects caller. A pool change needs a separate yes. |
| G5 re-verify parsers; one live call per backend | `dp.unverified`. `usage_layer.py read` is L1 and stays log-only. L2 is the seat plan's pinned calls. | Yes, before `backends_verified` is set true, and for every live call. |
| G6 panel forwarder | `dp.panel`. Keep the unit. Do not restart it here. | Yes, to repair or disable it. |
| G7 missing `paths.ip_schema` | `dp.schema_missing`. Code already fails closed on Hosts lines. | Yes, before creating the file. |
| G8 caps mismatch and the stale host label | `dp.caps`. Labels only. | Yes, before a config write. |
| G9 Cursor worker API key in process arguments | `dp.cursor_key`. Name only. Rotation is still open. | Yes. Independent of load balancing. |

## 9. Implementation and tests

Docs only in this pull request. Later code, in order, each behind the seat plan's gate:

1. Validator for `decision-script.json`. Tests: empty `rules` loads; a score weight, a throttle, or a `local` harness fails; a rebind of `dp.kill` fails; a rebind of `dp.panel` to `refuse` loads.
2. Front `route` over `route-choose`, plus the audit append. Tests: GO file absent still returns a ranking; KILL exits 3 with no audit of a call; a second `route` appends a second row and does not call `live.py`.
3. Front `dispatch` exactly-once wrapper around `live.py`, result file, session row. Tests: two dispatches of one id call the fake harness once; a different packet sha with the same id is `hold_entry` and does not call; `unknown_outcome` after a missing terminal row does not call.
4. Front `subscribe` over the log. Tests: `after_sequence` skips older rows; `until: terminal` exits on the terminal row; a cursor past the end is exit 2.
5. Router fixes already listed in the assessment (Grok timeout reads `timeout_s`, cooldown stays null unless set, quota hints do not fire on exit 0, full redacted reply, one router lock). Existing suites that pin `HEALTH_COOLDOWN_S is None` stay green.
6. Panel reads the audit log, only after the v02 files are in the repo. No new UI kit.

The seat plan's T1–T4 stay that plan's end-to-end checks. They run only after a yes to make live calls. This repo's current suite stays `python3 -m unittest discover -s tests` (286 tests, OK, on the step 1 commit; this step changes docs only).

## 10. OPEN QUESTIONS

1. Is a direction relayed for Jonathan the same bar as the seat plan's "Jonathan's own yes" on the caller-versus-pool ruling? This candidate treats it as the caller reading and still blocks a `route: true` on `grok-bot`.
2. Should `route` on a job id that was already dispatched return the stored decision, or a fresh ranking? This candidate recomputes `route` every time and never lets it overwrite the dispatch receipt.
3. Is at-most-once after a crash (section 5) acceptable, or is a human replay flag required? This candidate refuses automatic replay.
4. Is the coarse `state/locks/router` lock acceptable? It serializes every dispatch on ws1. Measured load is one real routed job. Per-harness locks would skip a busy account inside the walk, which is a selection change. Not adopted.
5. Should `cursor_key_exposed: true` refuse dispatch to cursor? This candidate only notifies. Refusing would change who is eligible.
6. The seat plan's risk 5 says a missing IP schema might send LAN work to other backends. `dispatcher.py` lines 518 and 523 fail closed on Hosts lines when the role set is empty. Which reading should a test lock, and is that test in scope given routing policy is out of scope?
7. Does `subscribe` need client-to-server prompt frames, or is a later `dispatch` on the same `session_id` enough? This candidate uses another dispatch, so the call list stays three.
8. The seat plan says leave the panel out of activation. The later direction says keep and extend it. This candidate keeps it and does not block submit on it. If "extend" means the panel must be up before LIVE, that reverses `dp.panel`'s default.

## 11. RISKS

1. A JSONL append plus fsync is not T3's one-transaction commit. Two front processes can interleave `seq`. The router lock does not cover `route` and `subscribe`. A reviewer who wants a single writer has to add the long-lived socket (open question 4) or accept torn lines as a failure (`reply_unreadable`).
2. Exactly-once plus the current hash job id will surprise callers. The seat plan tells the bot to resubmit after exit 2, 5, 7, or 8. After this candidate, exit 6 and exit 8 are consumed; exit 2 and exit 5 are not. A bot written to the seat plan will loop on a consumed id and get the same receipt.
3. `dp.quota` still sees today's `exit_map`: the hint list runs on the reply body (`harness_call.py` lines 104 and 121–124). A finished answer that contains those words becomes `failed_hold:usage` and the walk continues. The assessment's fix is not in this repo yet. Shipping the front first preserves that bug.
4. Grok's live path waits 600 s. One hung account dominates T1. The seat plan's end-to-end test will look like a hang.
5. `backends_verified: false` blocks every direct call, including the seat plan's T1, until someone sets the flag. That is deliberate. It also means a yes to "turn LIVE on" is not sufficient.
6. Including the v02 panel in the drift reconcile can pull in an unreviewed UI and a defaults file. Leaving it out, as the seat plan suggests, keeps activation smaller and ignores the later "keep and extend" direction.
7. The forced-command key is a root-equivalent for "anything the router will run". After PR #1 there is no per-packet GO line. The seat plan already names this. The decision script does not invent a replacement approval.
8. ws1 is one host. Load balancing here is across CLIs on that host. A down host is not failed over. The seat plan's risk 8 stands.
9. Public `main` and the tree ws1 runs are different commits. `dp.drift` will refuse every call until the manifest matches the tree that is actually on disk. A manifest written from public `main` while ws1 still runs the October tree blocks activation on purpose.
10. The Cursor worker API key is still in a process argument list on ws1, by the runtime receipt. This candidate does not rotate it, does not read it, and does not make load balancing depend on it. Treating it as in-scope work delays activation; leaving it for later leaves the exposure.
