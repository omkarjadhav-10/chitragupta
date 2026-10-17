"""Branch guard: PreToolUse hook for git branch creation and PR creation run through Claude Code.

    run.py hook pre-tool        (sync; stdin = the hook input)

When a new branch's parent, or a PR's base, doesn't fit the repo's strategy for that ticket, it acts on
branching.guard_mode:
  warn (default)  the command runs; Claude is told why it looks wrong (additionalContext), so it tells you at once
  ask             Claude Code asks you to confirm first (permissionDecision "ask")
  off             nothing
It never returns "allow" (that would skip your normal permission prompt) and never blocks.

Fast and fail-open: no git process (HEAD is read from .git), only config.json, the repo's branches.json / repo.json
and the ticket's JSON are read. Any error, or nothing to say: no output, exit 0.
"""
import json
import os
import sys

from . import common
from . import gitcmd

PREFIX = "Chitragupta branch guard"


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _short(ref, remote):
    ref = (ref or "").strip()
    for p in ("refs/heads/", "refs/remotes/"):
        if ref.startswith(p):
            ref = ref[len(p):]
    return ref[len(remote) + 1:] if ref.startswith(remote + "/") else ref


def check(cmd, cwd, cfg):
    """Messages for one command line (empty when it fits or isn't a branch / PR creation)."""
    s = gitcmd.settings(cfg)
    if s["guard_mode"] == "off":
        return []
    acts = [c for c in gitcmd.parse_command(cmd) if c["kind"] in ("create", "pr-create")]
    if not acts:
        return []
    root = common.find_repo_root(cwd)
    if not root:
        return []
    name = gitcmd.repo_key(root)
    conf = gitcmd.repo_conf(cfg, name)
    if not conf["configured"]:
        return []
    remote, bases = conf["remote"], conf["bases"]
    gdir = common.hp("data", "git", name)
    recs = _read_json(os.path.join(gdir, "branches.json"))
    manual = (_read_json(os.path.join(gdir, "repo.json")).get("manual") or {})
    prefixes = cfg.get("ticket_prefixes") or []
    current = common.read_branch(root)
    out = []

    def strategy(ticket):
        tk = _read_json(common.hp("data", "tickets", ticket + ".json")) if ticket else {}
        card = (_read_json(common.hp("data", "jira", ticket + ".json")).get("card") or {}) if ticket else {}
        nm, how = gitcmd.strategy_for(cfg, conf, manual, ticket, tk, (tk.get("jira") or {}).get("parent") or card.get("parent"))
        return nm, how, s["strategies"].get(nm) if nm else None

    def role(branch):
        r = gitcmd.role_of(branch, bases)
        if r:
            return r
        return (recs.get(branch) or {}).get("parent_role")

    for a in acts:
        if a["kind"] == "create":
            new = a.get("branch")
            ticket = common.ticket_from_branch(new, prefixes)
            if not ticket or gitcmd.ignored(cfg, ticket):
                continue
            parent = _short(a.get("from") or current or "", remote)
            if not parent or parent.startswith("detached@"):
                continue
            prole = role(parent)
            nm, how, strat = strategy(ticket)
            if not strat or not prole:
                continue
            roles = gitcmd.from_roles(strat)
            if prole not in roles:
                out.append("%s: new branch %s (%s) is being cut from %s (%s), but %s for %s starts from %s (strategy: %s)."
                           % (PREFIX, new, ticket, parent, prole, nm, ticket, " or ".join(sorted(r for r in roles if r)), how))
                continue
            want = gitcmd.suffix_role(new, conf["suffixes"])
            if want:
                src = [st.get("from") for st in strat["steps"] if st.get("pr_into") == want]
                if src and prole not in src:
                    out.append("%s: %s's name says it goes to %s, but it is cut from %s (%s); in %s a %s branch comes from %s."
                               % (PREFIX, new, want, parent, prole, nm, want, " or ".join(src)))
        else:
            base = _short(a.get("base") or "", remote)
            head = _short(a.get("head") or current or "", remote)
            if not base or not head:
                continue
            ticket = common.ticket_from_branch(head, prefixes) or (recs.get(head) or {}).get("ticket")
            if not ticket or gitcmd.ignored(cfg, ticket):
                continue
            nm, how, strat = strategy(ticket)
            hrole = (recs.get(head) or {}).get("parent_role")
            brole = gitcmd.role_of(base, bases)
            if not strat or not hrole or not brole:
                continue
            expect = [st.get("pr_into") for st in strat["steps"] if st.get("from") == hrole]
            if expect and brole not in expect:
                out.append("%s: PR from %s (cut from %s) into %s: %s for %s expects a %s-based branch to go into %s. "
                           "It would also carry the %s commits that %s doesn't have."
                           % (PREFIX, head, hrole, base, nm, ticket, hrole, " or ".join(expect), hrole, base))
            elif brole not in strat.get("targets") or []:
                out.append("%s: PR into %s, which isn't one of %s's targets (%s)."
                           % (PREFIX, base, nm, ", ".join(strat.get("targets") or [])))
    return out


def respond(msgs, mode):
    """The hook's stdout JSON (or None)."""
    if not msgs or mode == "off":
        return None
    text = "\n".join(msgs) + ("\nTell the user about this now, in one or two sentences, before going on." if mode == "warn" else "")
    hso = {"hookEventName": "PreToolUse"}
    if mode == "ask":
        hso["permissionDecision"] = "ask"
        hso["permissionDecisionReason"] = common.truncate("\n".join(msgs), 1500)
    else:
        hso["additionalContext"] = common.truncate(text, 4000)
    return {"hookSpecificOutput": hso}


def run(inp):
    tool = inp.get("tool_name") or ""
    if tool not in ("Bash", "PowerShell"):
        return None
    cmd = str((inp.get("tool_input") or {}).get("command") or "")
    if not cmd or not any(w in cmd for w in ("checkout", "switch", "branch", "pr create", "mr create")):
        return None  # the cheap filter, for Claude Code versions without the hook `if` field
    cfg = common.load_config()
    mode = gitcmd.settings(cfg)["guard_mode"]
    if mode == "off":
        return None
    return respond(check(cmd, inp.get("cwd") or os.getcwd(), cfg), mode)


def main(inp):
    out = run(inp)
    if out:
        sys.stdout.write(json.dumps(out, ensure_ascii=True))
        sys.stdout.flush()
