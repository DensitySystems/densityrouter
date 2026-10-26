# densityrouter usage

densityrouter by Density Systems (densitylabs.tech) - grok@densitylabs.tech - @densitylabsHQ

All commands run from `<dispatcher-dir>` with `python3`. Placeholders: `<dispatcher-dir>` (where the code lives), `<state-dir>` (`$DISPATCH_STATE`, else `paths.state_dir` from the config), `<ledger>` (GO ledger markdown), `<runs>` (RUNS TSV).

## Config
All paths, hosts and harness settings come from `densityrouter.config` (see README "The config"). Quick start:
```
cp densityrouter.config.example densityrouter.config   # then replace every <...>
python3 -c "import drconfig as C; print(C.config_path(), C.routed(), {h: C.run_host(h)['mode'] for h in C.routed()})"
```
Expected output (captured with `DENSITYROUTER_CONFIG=tests/fixtures/test.config`, exit 0):
```
tests/fixtures/test.config ['grok-build', 'cursor', 'agy-gemini'] {'grok-build': 'local', 'cursor': 'local', 'agy-gemini': 'local'}
```
With your own config, the first field is your config path and the lists show your `route: true` harnesses and their `run_host` modes.
Each variable below overrides the matching config value for that one process only; the config file is never changed.

## Environment
| variable | effect |
|---|---|
| `DENSITYROUTER_CONFIG` | path of the config file (must exist); default `densityrouter.config` next to the code; neither = exit 9 `CONFIG MISSING` |
| `DISPATCH_STATE` | state dir (leases/, pending/, LIVE, health.json, usage-ledger.jsonl, dispatch.log, jobs/) |
| `DISPATCH_LIVE` | `1` = live for this process only; anything else = off unless `<state-dir>/LIVE` exists |
| `DISPATCH_KILL` | path of the KILL file (default `<dispatcher-dir>/KILL`) |
| `DISPATCH_IP_SCHEMA` | markdown with the LAN host roles table (LAN rule B) |
| `DISPATCH_USAGE_DIR` | usage dir for `usage_writer.py` |
| `USAGE_LEDGER` | usage ledger path (default `<state-dir>/usage-ledger.jsonl`) |
| `GROK_BUILD_BIN`, `CURSOR_AGENT_BIN` | binary override, accepted only if it resolves to an allowlisted binary. Only these two are read; a `bin_env` on any other harness is a label |

## A packet
```
# PACKET: example

Objective: Fix the parser script in the scratch dir.
```
Write the Objective as an inline `Objective: ...` line (a `## Objective` heading is not read as the Objective). The task type comes from the Objective line (browser / research / refactor / code_edit). The GO ledger needs a line containing `| GO |`, the packet name and the first 12 hex of the packet's sha256, e.g. `| 12 | GO | PACKET-example.md <sha12> |`. The RUNS file needs a line containing the packet path or file name.

## dispatcher.py
```
python3 dispatcher.py adapters                         # harness owner labels and how to turn each off (KILL file)
python3 dispatcher.py capmap cursor                    # ability row for a harness (or a task type)
python3 dispatcher.py usage-snapshot [--usage-dir D]
python3 dispatcher.py route-choose <packet> [--usage-dir D]       # ranking, stdout only
python3 dispatcher.py dispatch --dry-run <packet> --ledger <ledger> --runs <runs> [--usage-dir D]
python3 dispatcher.py live <packet> --ledger <ledger> --runs <runs> [--usage-dir D] [--harness <h>]
```
`dispatch --dry-run` exits 0 ok, 2 HOLD, 3 KILLED, 8 packet redaction hits, 9 config missing. It never calls a CLI, even when live is on.

## live.py
```
python3 live.py <packet> --ledger <ledger> --runs <runs> [--usage-dir D] [--harness <h>]
python3 live.py --off                                   # rollback: remove <state-dir>/LIVE
```
Offline (default) it prints the handoff it would send and makes no calls. Live, it takes a lease, calls the chosen harness (falling back to the next on retryable failures), reads usage before/after, releases the lease, and prints JSON.
`--harness <h>` (operators and tests only) pins one harness after all checks; a pin to a non-route or LAN-barred harness exits 7. `--mode` and other permission-bypass flags are blocked for every CLI.

Exit codes: 0 done (or real PENDING, or offline would-dispatch); 2 HOLD; 3 KILLED; 5 lease refused; 6 not success; 7 pinned harness refused; 8 packet redaction hits; 9 config missing.

Examples:
```
# offline run: makes no calls only while live is off (no DISPATCH_LIVE=1 and no <state-dir>/LIVE); use dispatch --dry-run to be sure
python3 live.py packets/PACKET-example.md --ledger <ledger> --runs <runs>

# one live run in one process, temp state, nothing global
DISPATCH_STATE=$(mktemp -d) DISPATCH_LIVE=1 python3 live.py packets/PACKET-example.md --ledger <ledger> --runs <runs> --harness cursor

# turn live on for everyone using <state-dir>, then off
touch <state-dir>/LIVE
python3 live.py --off

# emergency stop / resume
touch <dispatcher-dir>/KILL
rm <dispatcher-dir>/KILL
```

## usage_layer.py
```
python3 usage_layer.py read grok-build          # one /usage reading (JSON); also cursor, agy-gemini
python3 usage_layer.py rollup --period daily    # or weekly; [--ledger <path>]
python3 usage_layer.py run <harness> --prompt "<text>" --bot-id <id> [--request-id R] [--ledger P] [--non-critical]
```
`read` opens the CLI interactively and types `/usage` (no model call). `run` makes a real model call with pre/post reads; `--non-critical` is recorded only (no throttle).

## usage_writer.py
```
python3 usage_writer.py [--usage-dir D] [--decl F] record <harness>
python3 usage_writer.py [--usage-dir D] [--decl F] snapshot <harness|all>
```

## harness_call.py
```
python3 harness_call.py <harness>     # one contract test call; prints the JSON result
```

## Tests
```
python3 -m unittest discover -s tests     # expected: Ran 286 tests, OK
```
Prerequisites: Python 3, git, coreutils (sha256sum, mktemp).
