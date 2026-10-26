#!/usr/bin/env python3
"""densityrouter central config. The ONE place for real locations (paths, hosts, state and ledger dirs)
and for every harness (names, CLI commands, where they run, account and plan labels). Stdlib only.

Lookup order for the file: $DENSITYROUTER_CONFIG, then <repo>/densityrouter.config. If neither exists the
router STOPS with ConfigMissing (exit 9 from the CLIs); it never falls back to the .example placeholders silently.
To run against the example on purpose (docs, smoke tests) set DENSITYROUTER_CONFIG=densityrouter.config.example.
Format: JSON. Keys starting with "_" are comments and are ignored. Relative paths resolve
against the repo folder; a leading "~" expands to the running user's home.
The older env overrides (DISPATCH_STATE, DISPATCH_IP_SCHEMA, DISPATCH_USAGE_DIR,
USAGE_LEDGER, GROK_BUILD_BIN, CURSOR_AGENT_BIN) still win over the config value when set."""
import json, os
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_ENV = "DENSITYROUTER_CONFIG"
REAL = os.path.join(HERE, "densityrouter.config")
EXAMPLE = os.path.join(HERE, "densityrouter.config.example")
_CACHE = {"key": None, "data": None}

class ConfigMissing(SystemExit):
    """Raised when no real config is found. Exits 9 with the reason on stderr (never a silent fallback)."""
    def __init__(self, msg):
        import sys
        super().__init__(9); self.msg = msg
        sys.stderr.write("densityrouter: CONFIG MISSING: %s\n" % msg)

def config_path():
    p = os.environ.get(CONFIG_ENV)
    if p:
        if not os.path.exists(p): raise ConfigMissing("$%s=%s does not exist" % (CONFIG_ENV, p))
        return p
    if os.path.exists(REAL): return REAL
    raise ConfigMissing("no densityrouter.config next to the code and $%s unset. Copy %s to %s and fill it in "
                        "(see README 'First run'); the .example is never used silently." % (CONFIG_ENV, os.path.basename(EXAMPLE), REAL))

def _strip(o):
    if isinstance(o, dict): return {k: _strip(v) for k, v in o.items() if not str(k).startswith("_")}
    if isinstance(o, list): return [_strip(v) for v in o]
    return o

def load(path=None):
    p = path or config_path()
    st = os.stat(p); key = (p, st.st_mtime_ns, st.st_size)
    if path is None and _CACHE["key"] == key: return _CACHE["data"]
    with open(p, encoding="utf-8") as f: data = _strip(json.load(f))
    if path is None: _CACHE.update(key=key, data=data)
    return data

def expand(p):
    """'~' -> home, relative -> under the repo folder. None/'' stays None."""
    if not p: return None
    p = os.path.expanduser(p)
    return p if os.path.isabs(p) else os.path.join(HERE, p)

def path(key, env=None):
    """paths.<key> from the config; an env var named by `env` wins when set."""
    if env and os.environ.get(env): return os.environ[env]
    return expand((load().get("paths") or {}).get(key))

def harnesses():
    """{name: settings} in file order (also the fallback order for routed harnesses)."""
    return dict(load().get("harnesses") or {})

def harness(h): return dict(harnesses().get(h) or {})

def routed():
    """Route candidates in fallback order: harnesses with "route": true."""
    return [h for h, v in harnesses().items() if v.get("route")]

def setting(section, key, default=None):
    return (load().get(section) or {}).get(key, default)

# ---------- where each harness CLI runs: local, or over ssh on a role-named host ----------
def run_host(h):
    """harnesses.<h>.run_host: {"mode": "local"} (default) or
    {"mode": "ssh", "host": "<role-host>", "ssh_opts": [...], "remote_cwd": "<dir on that host>"}."""
    r = harness(h).get("run_host") or {"mode": "local"}
    if r.get("mode", "local") not in ("local", "ssh"): raise ValueError(f"{h}: run_host.mode must be local or ssh")
    if r.get("mode") == "ssh" and not r.get("host"): raise ValueError(f"{h}: run_host.mode ssh needs host")
    return dict(r, mode=r.get("mode", "local"))

def is_remote(h): return run_host(h)["mode"] == "ssh"

def ssh_bin():
    import shutil
    b = os.path.expanduser(setting("ssh", "bin", "ssh") or "ssh")
    return b if os.path.isabs(b) else (shutil.which(b) or b)

def wrap(h, argv, tty=False):
    """Local: argv unchanged. ssh: [ssh, *opts, (-t), host, '--', '<quoted remote command>'].
    The remote command is the same argv, shell-quoted, run in run_host.remote_cwd when set. Pure."""
    import shlex
    r = run_host(h)
    if r["mode"] == "local" or argv is None: return argv
    q = lambda a: "~/" + shlex.quote(a[2:]) if a.startswith("~/") else shlex.quote(a)  # let the remote shell expand ~
    cmd = " ".join(q(a) for a in argv)
    if r.get("remote_cwd"): cmd = f"cd {q(r['remote_cwd'])} && {cmd}"
    return [ssh_bin(), *(r.get("ssh_opts") or []), *(["-t"] if tty else []), r["host"], "--", cmd]
