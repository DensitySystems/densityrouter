# densityrouter terminology

Use these terms, with these meanings, in code comments, user-facing strings and docs. Identifiers, config keys, flags, file names and exit codes keep their existing names.

## Core terms
| term | meaning |
|---|---|
| router | densityrouter as a whole: it chooses a harness for a packet and, when live, runs it |
| harness | one AI coding CLI configured under `harnesses.<name>` in `densityrouter.config` |
| packet | a markdown job file; its `Objective:` line sets the task type |
| task type | one of `browser`, `research`, `refactor`, `code_edit` |
| candidate | a harness with `"route": true` that passed the hard rules |
| hard rule | a check that removes a candidate or stops the run: KILL file, LAN rule, held lease |
| score | `A^0.6 × U^0.4 × H` for a candidate |
| ability (A) | the 0-1 value for a harness and task type in `capability-map.json` |
| usage headroom (U) | the usage term of the score, computed from the latest usage snapshot |
| health (H) | the health term of the score; 1.0 unless a cooldown is configured |
| fallback | trying the next ranked candidate after a retryable status |
| fallback order | the order of route candidates in the config file, used on an exact tie or when every score is unknown |
| pin | `--harness <h>`: run only that harness; no fallback |
| dry run | a run that checks and ranks but sends nothing (`dispatch --dry-run`, or `live.py` while live is off) |
| live | the mode in which the router calls a harness: `DISPATCH_LIVE=1` or `<state-dir>/LIVE` |
| offline | the default mode: no calls |
| approval | a GO ledger line naming the packet file and its sha12 |
| GO ledger | the markdown file that holds approvals |
| RUNS file | the TSV file that must list the packet |
| sha12 | the first 12 hex characters of a file's SHA-256 |
| lease | a file that marks a job as in flight so no second router takes it |
| KILL file | the emergency stop file; every command checks it first and exits 3 |
| usage read | one capture of a CLI's own `/usage` screen |
| usage snapshot | the per-harness JSON file in the usage dir used for scoring |
| usage ledger | the JSONL file with one row per live call |
| pool | the usage allowance a harness draws from |
| label | a config value shown in output that never affects routing |
| redaction | masking of secrets in all output; a packet that contains one is refused (exit 8) |
| HOLD | a check failed and nothing was sent |
| PENDING | an async harness accepted the job; the result arrives later as a receipt file |
| NOT_DISPATCHED | the harness has no `invoke` command |

## Style
- State what the code does. Do not describe how it used to behave, how good it is, or what is planned.
- Use plain words; define any abbreviation at first use.
- Give numbers with units (`600 s`, `24 h`, `75%`).
- Write "falls back to the next candidate", "has no effect", "is not used".
- Refer to reviews, tickets and work logs nowhere in shipped files.
