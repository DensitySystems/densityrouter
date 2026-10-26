![densityrouter](docs/splash.jpg)

# densityrouter

densityrouter by Density Systems (densitylabs.tech) - grok@densitylabs.tech - @densitylabsHQ

Licensed under the Apache License, Version 2.0 (see `LICENSE` and `NOTICE`).

## What it is
densityrouter is a small, stdlib-only Python 3 router. It picks which AI coding CLI (a "harness") should run a packet (a markdown job file), checks that the packet is approved, and, only when live mode is on, sends it to that CLI and records what happened.

Configuration comes from `densityrouter.config` plus the two data files `capability-map.json` and `caps.json` that ship next to the code; the environment variables in USAGE.md can override single values for one process. At run time it also reads the packet, the GO ledger, the RUNS file, its state dir and the harness CLIs' own `/usage` screens. It never calls into any other bot or agent.

Harnesses shipped in the example config: `grok-build` (Grok Build CLI `grok`), `cursor` (Cursor CLI `cursor-agent`), `agy-gemini` (AGY CLI `agy`). Some code paths are still specific to these three names; see "Adding a harness".

Default is OFFLINE (dry run): it prints what it would send and makes zero calls.

## Install prerequisites
- Python 3, standard library only (no `pip install`). Tested on CPython 3.13.5 and 3.14.4; older versions are untested.
- Linux (usage reads use a pseudo-terminal and `/proc`).
- `ssh` client, only if a harness has `run_host.mode: "ssh"` (key login must already work non-interactively).
- Each harness CLI you want to route to, installed and already logged in by you under the OS user that runs it. Logging in, installing and CLI credentials are out of scope: the router never installs, logs in, reads credentials or changes CLI config. A missing CLI reports `failed_hold:not_installed` and the router falls back to the next candidate.

## Get the code
```
git clone <repo-url> densityrouter && cd densityrouter     # or unpack a release archive
```

## First run (offline, no calls)
```
cp densityrouter.config.example densityrouter.config       # then replace every <...> you need
mkdir -p state ledger packets                                 # state_dir, go_ledger/runs folder, packets_dir from the example
printf '# PACKET: hello\n\nObjective: Read README.md and reply with a 3-line summary. Read-only: do not edit any file.\n' > packets/PACKET-hello.md
S=$(sha256sum packets/PACKET-hello.md | cut -c1-12)
printf '| 1 | GO | PACKET-hello.md %s |\n' "$S" > ledger/GO-LEDGER.md   # GO ledger format
printf 'hello\tpackets/PACKET-hello.md\n' > ledger/RUNS.tsv            # RUNS format: any row naming the packet
python3 dispatcher.py dispatch packets/PACKET-hello.md --dry-run; echo "exit=$?"
```
Expected: JSON with `"status": "WOULD DISPATCH (dry-run, nothing sent)"`, `"task_type": "research"`, then `exit=0`. Before any usage snapshot exists it also prints `No usage snapshots: routing by ability only (U = 0.3 default)`. Editing the packet changes its sha12, so the GO line must be redone.

## The config
`densityrouter.config` is JSON and the single reference for every real location (state, ledger, RUNS, usage and packet folders, LAN schema), every host, and every harness. It is git-ignored; the repo ships `densityrouter.config.example`.
- Lookup: `$DENSITYROUTER_CONFIG` (must exist), else `densityrouter.config` next to the code. If neither exists the router stops with `CONFIG MISSING` (exit 9). The `.example` is never used silently.
- Relative paths resolve under the code folder; `~` expands to the home of the user running the router.
- `paths`: `state_dir`, `go_ledger`, `runs`, `usage_dir`, `packets_dir`, `ip_schema`.
- `lan.harness`: the only harness allowed for LAN work. Unset = LAN work routes nowhere (HOLD).
- `harnesses.<name>`: `route`, `bin`, `bin_env` + `allow_bins`, `invoke` and `request_argv` (`{bin}` and `{prompt}` are filled in), `timeout_s`, `async`, `pool`, `usage` (`parser`, `primary`, `reader_cwd`), `run_host`.
- Label-only keys (shown in output, never change routing): `owner`, `off`, `account`, `subscription`, `desktop_only`, `host`, `route_via`, `usage.source`, `usage.report_step`. For the `host` / `route_via` labels shown in route output, `capability-map.json` wins over the config. Where a CLI actually runs is decided only by `run_host` (local, or ssh to `run_host.host`); `desktop_only` and `host` never affect it.
- Environment overrides: every variable in USAGE.md "Environment" overrides the matching config value for that one process only; the config file is never changed.

## How routing works
1. Hard rules, which can refuse a candidate: the KILL file (exit 3); LAN (only `lan.harness` may take LAN work); a live lease on the job (no reroute while held); `route: false` harnesses are never candidates.
2. Score = `A^0.6 × U^0.4 × H`. A = ability for the task type (`capability-map.json`, 0-1; no entry = no score). U and H are defined exactly below.

   **U (usage headroom)**, from `<usage_dir>/<harness>.json` (`dispatcher.usage_for`). rem = `remaining_pct / 100`.
   - No snapshot (or no usage dir) = 0.3 (`unknown`). Usage dir exists but the writer has no declared allowance / `window_len_h` / `reset_at` = 0.0 (`config-gap`). Snapshot older than 24 h = 0.3 (`unknown(>24h)`).
   - Otherwise, with burn = `burn_pct_per_h` from the snapshot, else the observed rate from the usage counter, else none: if burn is known, tl = hours to reset (floored at 0), eu = rem − burn/100 × tl, w = `window_len_h`, and when eu > 0 and w > 0: prox = clamp(1 − tl/w, 0, 1) and U = rem × (1 + 0.2224 + eu × prox). In every other case U = rem. A date-only or unknown reset from the CLI drops the burn (U = rem).
   - Snapshot older than 6 h (and at most 24 h) halves U (`stale`). There is no upper clamp.

   **H (health)**. With the shipped default `HEALTH_COOLDOWN_S = None`, H = 1.0 for every candidate; recent failures in `<state-dir>/health.json` are reported as a gap but do not lower H or skip. If a cooldown N seconds is set, a harness whose recorded failure is younger than N is skipped (not scored); otherwise H = 1.0. Only a caller passing its own health dict to `route()` (tests) gets H = that value (default 1.0).
3. Fallback order (route candidates in config file order) is used only on an exact tie or when every score is unknown.

Caps (`caps.json`) are not a hard rule: `enabled`, `daily_cap`, `used_today` and `test_call_cap` never block or skip a call (the `enabled` key has no effect). Nothing resets `used_today`; it is a manual note.

Task type: taken from the packet's `Objective:` line(s); without one, from the whole text minus Basis/Input/Boundaries/NOT covered/Out of scope/Scars/Sources sections. Negated clauses (`do not …`, `don't …`, `never …`, `without …`, up to `.`/`;`) are removed first, and lines saying something is barred/forbidden/untouched/stays off are dropped. Then the first match wins: browser (`browser`, `web page`, `website`), research (`research`, `eval`, `read-only`, `summary/summarize`), refactor (`refactor`, `repo-wide`), code_edit (`code`, `build`, `script`, `fix`); no match = code_edit.

LAN: only from declared `Placement:` / `Network placement:` / `Hosts:` / `Host:` lines or the Objective line: the word `LAN`, or a host that names a role in the `ip_schema` table. The body is not scanned.

Retryable (the router falls back to the next candidate): `failed_retryable`, `timed_out`, `failed_hold:usage`, `failed_hold:not_installed`, and (unpinned) `NOT_DISPATCHED`. Done, PENDING and every other status stop the fallback.

## Files
| file | role |
|---|---|
| `dispatcher.py` | routing, hard rules, leases, GO/RUNS checks, redaction, CLI |
| `live.py` | live caller (offline unless live is on) |
| `harness_call.py` | runs one harness CLI with timeout, exit mapping, redaction |
| `usage_layer.py` | `/usage` reads and parsers, usage ledger, rollups |
| `usage_writer.py` | writes usage snapshots for routing |
| `drconfig.py` | loads `densityrouter.config` |
| `densityrouter.config.example` | the config template |
| `capability-map.json` | ability per harness per task type (data) |
| `caps.json` | per-harness labels/counters (data; never blocks) |
| `usage-schema.json` | usage snapshot schema |
| `tests/` | stdlib `unittest` suites, using `tests/fixtures/test.config` |
| `docs/TERMINOLOGY.md` | the terms and writing style used in this repo |

## State layout
`<state-dir>` = `$DISPATCH_STATE`, else `paths.state_dir` (create it with `mkdir -p`):
```
<state-dir>/LIVE                 empty file: live ON for every process using this state dir
<state-dir>/leases/<job>.json    in-flight leases
<state-dir>/pending/<job>.json   async jobs waiting for a receipt
<state-dir>/jobs/<job>/          working folder each live call runs in
<state-dir>/calls.json           per-harness call counters
<state-dir>/health.json          recent failures (cooldown input; H stays 1.0 while HEALTH_COOLDOWN_S is unset)
<state-dir>/dispatch.log         one line per live call
<state-dir>/usage-ledger.jsonl   one row per live call (or $USAGE_LEDGER)
<state-dir>/usage-alerts.log     75% / 90% alert lines
<code-dir>/KILL                  emergency stop; lives next to the code, not in state (or $DISPATCH_KILL)
```

## Adding a harness
Config and data, always:
1. `densityrouter.config`: a `harnesses.<name>` entry (copy one). `invoke` null = `NOT_DISPATCHED`.
2. `capability-map.json`: `"<name>": {"code_edit": 0-1, "refactor": 0-1, "research": 0-1, "browser": 0-1}`.
3. `caps.json`: `"<name>": {"enabled": true, "used_today": 0}` (labels only).
4. `usage-schema.json`, if you validate snapshots for it.

Code that still names the shipped harnesses (edit if your harness needs the same behaviour):
- `harness_call.py` lines ~69-70: pinned binaries / env overrides for `grok-build` and `cursor` only.
- `harness_call.py` lines ~229-249: cursor-only agent guard, missing-binary hold and "not logged in" detection.
- `usage_layer.py` `_PARSER_FUNCS`: one parser per `/usage` screen format (`grok`, `cursor`, `agy`); `usage.parser` names one.
- `usage_layer.py`: grok-only session token lines (`parse_grok_session`, `GrokSession`, `grok sessions list` / `grok usage`).
- `live.py` line ~174: the persistent usage session is used only for `grok-build`.
- `dispatcher.py`: the LAN harness is config (`lan.harness`), not code.

Parser contract: `parse(text, now) -> {pool_name: {"used_pct" or "remaining_pct": float, "reset_at": iso or "unknown", "window": str, ...}}`; return `{}` when the screen is not recognised. Return no value rather than an estimate. Without a parser a harness's usage is `unknown`, which gives U = 0.3 in the score (neutral: neither preferred nor avoided).

## Secrets
- CLI logins and tokens are out of scope: they stay in each CLI's own store. Put no secrets in the config (`account` / `subscription` are labels).
- Redaction patterns live in `dispatcher.py` (`SECRET_RES`, `LONGHEX`): bearer tokens, private keys, common key prefixes, long hex. All output passes through redaction.
- A packet whose text matches any pattern is refused before any call: exit 8.

## Usage logging (log-only)
- Each live call reads the CLI's own `/usage` just before and just after the call, in background threads. Reads never block a request. A read that fails, throws or passes the 45 s limit is logged as `parse_ok: false` / `read_failed` and the call result is still written.
- If the CLI exits before its `/usage` screen is captured, the read is logged as `read_failed` with error `CliExitedEarly`.
- One JSON row per call in the usage ledger, plus one `dispatch.log` line. Tokens and cost appear only where the CLI prints them, otherwise `unknown`.
- Alerts at 75% and 90% used: one line in `usage-alerts.log`. Nothing else happens.
- Rollup: `python3 usage_layer.py rollup --period daily|weekly`.

## Live on/off, KILL and rollback
- Live is ON only when env `DISPATCH_LIVE` is exactly `1`, or `<state-dir>/LIVE` exists.
- Turn on: create `<state-dir>/LIVE`, or set `DISPATCH_LIVE=1` for one process. There is no "on" command.
- Turn off: `python3 live.py --off` (removes `<state-dir>/LIVE`; tells you to unset `DISPATCH_LIVE` if set).
- Emergency stop: create `<code-dir>/KILL` (or `$DISPATCH_KILL`). Every command checks it first and exits 3.
- Code rollback (git release): `python3 live.py --off`, confirm `<state-dir>/leases/` is empty, then `git checkout <previous-tag-or-sha>` in the code dir; your `densityrouter.config` and `state/` are git-ignored and stay. Run one dry run.
- `--harness <h>` (pin) is for operators and tests only. `--mode` and other permission-bypass flags are blocked for every CLI.

## Tests
```
for t in tests/test_*.py; do python3 "$t" || echo "FAIL $t"; done
```
Or all at once: `python3 -m unittest discover -s tests` (expected: `Ran 286 tests`, `OK`). Each suite prints `Ran N tests` and `OK`; no `FAIL` lines, no skips. The suite forces `DENSITYROUTER_CONFIG` to `tests/fixtures/test.config`, where every CLI is `/bin/false`, so an exported real config is ignored. Tests use temp state and fake CLIs only; they make no real calls.

## Known limits
- Throttle is not built: `THROTTLE_PCT = None`; usage never slows or blocks a request. Health cooldown is unset.
- No per-account switch inside one CLI. A second harness entry with the same `bin`, OS user and `run_host` uses the same login and the same usage meter. To meter a second account separately, give the entry its own login: a different OS user or `run_host` host where that CLI is logged in to the other account (the router never logs in for you).
- Some code paths name the shipped harnesses (see "Adding a harness").

## Repository
`<repo-url>` (set when published).

## Contact
grok@densitylabs.tech · @densitylabsHQ on X
