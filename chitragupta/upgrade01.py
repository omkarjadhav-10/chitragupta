"""Upgrade 01 migration: replayable requests, DB flow maps, artifacts.

    upgrade01.py --dry-run     show what the migration will change (nothing written)
    upgrade01.py --apply       schema_version 1 -> 2, config keys, literal values in bruno/ -> {{process.env.*}}

Idempotent: running --apply twice changes nothing the second time.
"""
import difflib
import json
import os
import re
import sys

from . import apicalls
from . import brufile
from . import common
from . import gitsync
from .store import Store

SCHEMA_VERSION = 2


def watch_dirs():
    """Artifact watch folders for a config that has no `artifacts` block yet (config artifacts.watch_dirs)."""
    return list((common.load_config().get("artifacts") or {}).get("watch_dirs") or [])


def meta_path():
    return common.hp("data", "meta.json")


def literal_conversions(path):
    """(new_text, [(VAR, value)]) for one .bru: literal credentials / person values in the JSON body -> {{process.env.*}}."""
    txt = common.read_text(path)
    req = brufile.parse_request(txt)
    if not req or req.get("body") is None or (req.get("body_kind") or "json") != "json":
        return txt, []
    try:
        body = json.loads(req["body"])
    except ValueError:
        return txt, []
    ticket = os.path.basename(os.path.dirname(path))
    vs = apicalls.Vars(ticket)
    templ = apicalls.template_json(body, vs)
    # credentials follow the request's existing password variable: ABC1001_PASSWORD -> ABC1001_EMAIL
    m = re.search(r"\{\{process\.env\.(\w+?)_PASSWORD\}\}", req["body"])
    pairs = []

    def walk(a, b, keypath):
        if isinstance(a, dict) and isinstance(b, dict):
            for k in a:
                walk(a[k], b.get(k), keypath + [k])
        elif isinstance(a, list) and isinstance(b, list):
            for x, y in zip(a, b):
                walk(x, y, keypath)
        elif a != b and isinstance(b, str) and b.startswith("{{process.env."):
            var = b[len("{{process.env."):-2]
            if m and apicalls.LOGIN_ID_RX.match(str(keypath[-1])):
                var = "%s_%s" % (m.group(1), apicalls.snake(keypath[-1]).upper())
            pairs.append((var, a, keypath[-1]))
    walk(body, templ, [])
    if not pairs:
        return txt, []
    i = txt.find("body:json {")
    j = txt.find("\n}", i)
    seg = txt[i:j]
    used = {}
    for var, val, key in pairs:
        lit = json.dumps(val, ensure_ascii=False)
        pat = re.compile(r'("%s"\s*:\s*)%s' % (re.escape(key), re.escape(lit)))
        if not pat.search(seg):
            continue
        n = used.get(var, 0)
        used[var] = n + 1
        name = var if n == 0 else "%s_%d" % (var, n + 1)
        seg = pat.sub(lambda mm: mm.group(1) + '"{{process.env.%s}}"' % name, seg, count=1)
    new = txt[:i] + seg + txt[j:]
    out_pairs = []
    used = {}
    for var, val, key in pairs:
        n = used.get(var, 0)
        used[var] = n + 1
        out_pairs.append((var if n == 0 else "%s_%d" % (var, n + 1), str(val)))
    return new, out_pairs


def plan():
    changes = []
    root = common.hp("bruno")
    if os.path.isdir(root):
        for t in sorted(os.listdir(root)):
            d = os.path.join(root, t)
            if not os.path.isdir(d) or t == "environments":
                continue
            for fn in sorted(os.listdir(d)):
                if fn.endswith(".bru") and fn != "folder.bru":
                    p = os.path.join(d, fn)
                    new, pairs = literal_conversions(p)
                    if pairs:
                        changes.append((p, common.read_text(p), new, pairs))
    return changes


def apply_env(pairs):
    env_path = common.hp("bruno", ".env")
    ex_path = common.hp("bruno", ".env.example")
    cur = common.read_text(env_path)
    have = set(re.findall(r"(?m)^\s*([A-Za-z_]\w*)\s*=", cur))
    add = ["%s=%s" % (k, v) for k, v in pairs if k not in have]
    if add:
        common.write_text(env_path, (cur.rstrip("\n") + "\n" if cur.strip() else "") + "\n".join(dict.fromkeys(add)) + "\n")
    ex = common.read_text(ex_path)
    exh = set(re.findall(r"(?m)^\s*([A-Za-z_]\w*)\s*=", ex))
    add_ex = list(dict.fromkeys("%s=" % k for k, _ in pairs if k not in exh))
    if add_ex:
        common.write_text(ex_path, ex.rstrip("\n") + "\n" + "\n".join(add_ex) + "\n")


def migrate(dry):
    out = []
    meta = common.read_json(meta_path(), None) or {}
    if meta.get("schema_version", 1) < SCHEMA_VERSION:
        out.append("data/meta.json: schema_version %s -> %d" % (meta.get("schema_version", 1), SCHEMA_VERSION))
    cfg = common.read_json(common.wl("config.json"))
    models = cfg.setdefault("models", {})
    for k in ("curate", "dbflow"):
        if k not in models:
            out.append("config.json: models.%s = sonnet" % k)
            models[k] = "sonnet"
    if "artifacts" not in cfg:
        wd = watch_dirs()
        out.append("config.json: artifacts = {watch_dirs: %s, push_binary_types: []}" % wd)
        cfg["artifacts"] = {"watch_dirs": wd, "push_binary_types": [], "text_max_bytes": 2000000, "binary_max_bytes": 25000000}
    conv = plan()
    for p, old, new, pairs in conv:
        rel = os.path.relpath(p, common.home()).replace("\\", "/")
        out.append("%s: %s" % (rel, ", ".join("%s" % k for k, _ in pairs)))
        if dry:
            out += ["    " + l for l in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0) if not l.startswith(("---", "+++"))]
    if dry:
        return out
    for p, old, new, pairs in conv:
        apply_env(pairs)  # values first, so the request keeps working
        common.write_text(p, new)
    st = Store()
    for t in Store.all_ticket_ids():
        st.ticket(t)
        st.touch_ticket(t)
    st.save()
    common.write_json(common.wl("config.json"), cfg)
    common.ensure_dir(common.hp("_drop"))
    gitsync.ensure_repo(common.load_config())
    common.write_json(meta_path(), {"schema_version": SCHEMA_VERSION, "upgraded": common.now_iso(),
                                    "features": ["replay_requests", "db_flow", "artifacts"]})
    return out


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    dry = "--apply" not in sys.argv
    for line in migrate(dry):
        print(line)
    print("(dry run - nothing written)" if dry else "migration applied")
