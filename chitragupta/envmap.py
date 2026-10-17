"""Request host -> environment (qa / dev / local / uat / staging / prod). Anything unknown counts as production.

Hosts are learned from the Bruno environments (bruno/environments/<ENV>.bru of the worklog collection and of the
configured collections) plus `qa_data.hosts` in config ({"host": "qa"}). The map is cached in
.worklog/cache/envhosts.json so the hook can read it without parsing Bruno files.
"""
import os
import re

from . import common

_HOST = re.compile(r"https?://([^/\s'\"{}:]+)(?::\d+)?", re.I)
ENV_NAMES = {"qa": "qa", "dev": "dev", "local": "local", "uat": "uat", "stg": "staging", "staging": "staging",
             "prod": "prod", "production": "prod"}
CACHE = ("cache", "envhosts.json")


_TOKEN = re.compile(r"(?:^|[.-])(qa|dev|uat|stg|staging|prd|prod|production)(?=[.-]|$)")


def host_token(host):
    """Environment named in the hostname itself (api-qa.acme.test, shop-stg.acme.test), else None."""
    if host in ("localhost", "127.0.0.1"):
        return "local"
    m = _TOKEN.findall(host.split(":")[0])
    if not m:
        return None
    e = m[0]
    return {"stg": "staging", "prd": "prod", "production": "prod"}.get(e, e)


def build(cfg):
    hosts = {}
    dirs = [common.hp("bruno", "environments")] + [os.path.join(c, "environments") for c in cfg.get("bruno_collections") or []]
    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for fn in names:
            if not fn.lower().endswith(".bru"):
                continue
            env = ENV_NAMES.get(fn[:-4].lower())
            if not env:
                continue
            for h in _HOST.findall(common.read_text(os.path.join(d, fn))):
                h = h.lower()
                named = host_token(h)
                if named:
                    hosts[h] = named
                elif h not in hosts or hosts[h] == "local":  # a LOCAL env file also lists the QA hosts it calls
                    hosts[h] = env
    for h, e in ((cfg.get("qa_data") or {}).get("hosts") or {}).items():
        hosts[h.lower()] = e
    for h in ("localhost", "127.0.0.1"):
        hosts.setdefault(h, "local")
    common.write_json(common.wl(*CACHE), hosts)
    return hosts


def load():
    return common.read_json(common.wl(*CACHE), {}) or {}


def env_of_host(host, hosts=None):
    hosts = load() if hosts is None else hosts
    return hosts.get((host or "").lower(), "prod")


def env_of_text(text, hosts=None):
    """Environment of every URL in a command/file: one env when all agree, 'prod' when any is unknown, None if no URL."""
    found = {env_of_host(h, hosts) for h in _HOST.findall(text or "")}
    if not found:
        return None
    return found.pop() if len(found) == 1 else ("prod" if "prod" in found else "mixed")


def allowed(env, cfg):
    return env in ((cfg.get("qa_data") or {}).get("allowed_envs") or ["qa"])
