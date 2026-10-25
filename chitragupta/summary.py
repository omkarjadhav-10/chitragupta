"""Six-question ticket summary. Facts are rebuilt from data; model prose must cite current evidence."""
import re

from . import common

ORDER = ("problem", "root_cause", "changed", "verified", "unknowns", "next_action")
HEADINGS = dict(zip(ORDER, (
    "What is the problem?", "What is the current root cause, and how certain is it?", "What changed?",
    "What was actually verified?", "What remains blocked or unknown?", "What is the next action?")))


def _short(text, words=32):
    text = re.sub(r"<[^>]+>", " ", str(text or ""))
    parts = text.split()
    return " ".join(parts[:words]) + ("…" if len(parts) > words else "")


def _claim(text, cites):
    return {"text": _short(text), "cites": list(dict.fromkeys(cites))}


def _known(tk, key, card):
    from . import brief
    ids = {i["id"] for i in brief.current_items(tk)}
    for c in brief._commits(tk):
        ids.update("commit:" + s for s in [c["sha7"]] + c["aliases"])
    ids.update("pr:%s" % p["number"] for p in brief._prs(tk))
    for kind, prefix in (("tests", "test:"), ("queries", "query:"), ("tables", "table:")):
        for rid, row in (tk.get(kind) or {}).items():
            if isinstance(row, dict) and not row.get("archived") and not row.get("review") and row.get("relevance") != "noise":
                ids.add(prefix + (row.get("id") or rid))
    ids.update("replay:%s" % r["n"] for r in (tk.get("replay") or {}).get("final") or [] if r.get("n") is not None)
    if card.get("found") or tk.get("jira") or tk.get("title_source") == "jira":
        ids.add("jira:" + key)
    return ids


def validate_model(raw, key, tk, card):
    from . import brief
    ctx, dropped = brief._Ctx(key, tk, card), []
    ctx.known &= _known(tk,key,card)
    out = {"unknowns": [], "next_action": None}
    if not isinstance(raw, dict):
        return out, dropped
    def valid(row, section):
        if not isinstance(row, dict) or not row.get("text"):
            return None
        text = " ".join(str(row["text"]).split())
        cites = brief._norm_cites(row.get("cites"), ctx.items)
        why = ctx.cite_problem(cites) or ctx.names(text)
        if not why and len(text.split()) > 45:
            why = "too-long"
        if why:
            dropped.append({"section": section, "text": common.truncate(text,200), "why": why})
            return None
        return _claim(text,cites)
    for row in raw.get("unknowns") or []:
        c = valid(row,"unknowns")
        if c and len(out["unknowns"]) < 3:
            out["unknowns"].append(c)
    out["next_action"] = valid(raw.get("next_action"),"next_action")
    return out, dropped


def _verification(tk, key, cfg):
    from . import brief, relevance, store
    rows = []
    tests = sorted((r for r in (tk.get("tests") or {}).values() if isinstance(r,dict) and not r.get("archived") and r.get("id")
                    and r.get("status") in ("passed","failed")), key=lambda r: r.get("at") or "", reverse=True)
    for t in tests[:3]:
        rows.append(_claim("%s: %s" % (t.get("command") or "Recorded test",t["status"]), ["test:" + t["id"]]))
    for step in (tk.get("replay") or {}).get("final") or []:
        call = (tk.get("api_calls") or {}).get(step.get("call_id")) or {}
        status = call.get("status") or step.get("status")
        if step.get("executed") is True and status is not None and step.get("n") is not None:
            rows.append(_claim("%s: HTTP %s" % (step.get("name") or "Replay",status),["replay:%s" % step["n"]]))
    for q in relevance.core(tk,"queries"):
        if q.get("role") == "verify" and q.get("sent") is True and not relevance.NORES.search(q.get("revealed") or ""):
            rows.append(_claim(q.get("revealed"),["query:" + q["id"]]))
    status = store.jira_status(tk)
    if brief.qa_verified(status,cfg):
        rows.insert(0,_claim("Verified by QA (Jira: %s)" % status,["jira:" + key]))
    return rows[:3]


def _assemble(tk, key, cfg, card, sections, model):
    from . import brief, delivery
    items = brief.current_items(tk)
    known = _known(tk,key,card)
    def current(rows):
        return [dict(c, text=_short(c.get("text"))) for c in rows or [] if isinstance(c,dict) and c.get("text") and c.get("cites")
                and all(cid in known for cid in c["cites"])]
    sec = {s: {"claims": []} for s in ORDER}
    problem = card.get("problem") or card.get("description") or tk.get("problem") or ""
    if problem and "jira:" + key in known:
        sec["problem"]["claims"] = [_claim(problem,["jira:" + key])]
    root = (sections or {}).get("root_cause") or {}
    sec["root_cause"] = {"claims": current(root.get("claims")), "confidence": root.get("confidence") or "none"}
    ov = brief.overrides(tk).get("root_cause")
    if ov:
        sec["root_cause"] = {"claims": [_claim(ov[1],[ov[0]["id"]])],"confidence":"confirmed"}
    elif not sec["root_cause"]["claims"]:
        causes = [i for i in items if i.get("kind") == "root_cause"]
        causes.sort(key=lambda i: brief.CONF_RANK.get(i.get("confidence"),0),reverse=True)
        if causes:
            i = causes[0]
            sec["root_cause"] = {"claims": [_claim(i.get("title"),[i["id"]])],"confidence":i.get("confidence") or "hypothesis"}
    for group in brief._commits(tk):
        text = group["subject"]
        if group.get("reached"):
            text += "; reached " + ", ".join(group["reached"])
        prs = [p for p in brief._prs(tk) if p.get("source") in group.get("branches",[]) or p.get("target") in group.get("reached",[])]
        if prs:
            text += "; " + ", ".join("PR #%s %s" % (p["number"],p.get("state") or "") for p in prs[:2])
        sec["changed"]["claims"].append(_claim(text,["commit:" + group["sha7"]] + ["pr:%s" % p["number"] for p in prs[:2]]))
    sec["changed"]["claims"] = sec["changed"]["claims"][:3]
    sec["verified"]["claims"] = _verification(tk,key,cfg)
    sec["unknowns"]["claims"] = current(model.get("unknowns"))
    for i in items:
        if i.get("kind") in ("risk","question","blocker") and len(sec["unknowns"]["claims"]) < 3:
            if not any(i["id"] in c["cites"] for c in sec["unknowns"]["claims"]):
                sec["unknowns"]["claims"].append(_claim(i.get("title"),[i["id"]]))
    stage = delivery.stage(tk.get("git") or {})
    groups = brief._commits(tk)
    if stage and stage.get("gaps") and groups:
        sec["unknowns"]["claims"] = [_claim("Missing delivery: " + "; ".join(str(g) for g in stage["gaps"]),["commit:" + groups[0]["sha7"]])] + sec["unknowns"]["claims"][:2]
    action = current([model.get("next_action")])
    # Owner next-action notes take precedence, then the model's grounded proposal, then an open TODO.
    for i in items:
        if i.get("kind") == "note" and (i.get("session") == "inbox" or i.get("src") == "user"):
            m = re.match(r"(?i)next(?: action| step)?\s*:\s*(.+)",i.get("detail") or i.get("title") or "")
            if m:
                action = [_claim(m.group(1),[i["id"]])]
                break
    if not action:
        todo = next((i for i in items if i.get("kind") == "todo"),None)
        if todo:
            action = [_claim(todo.get("title"),[todo["id"]])]
    sec["next_action"]["claims"] = action[:1]
    # At most three bullets per question, around 150–250 words including headings. Never invent filler.
    budgets = dict(problem=25,root_cause=50,changed=35,verified=30,unknowns=25,next_action=25)
    for sid, section in sec.items():
        budget, rows = budgets[sid], []
        for c in section["claims"][:3]:
            if budget <= 0:
                break
            c = dict(c,text=_short(c["text"],budget))
            budget -= len(c["text"].split())
            rows.append(c)
        section["claims"] = rows
    return {"v":1,"sections":sec,"next_action":(sec["next_action"]["claims"] or [None])[0]}


def build(tk,key,cfg,sections,raw=None,card=None):
    from . import brief
    card = brief.jira_card(key) if card is None else card
    model,dropped = validate_model(raw,key,tk,card)
    return _assemble(tk,key,cfg,card,sections,model),dropped


def view(tk,key=None,cfg=None,card=None,sections=None):
    """Cheap current-id validation at read time; never constructs the generation corpus."""
    from . import brief
    key = key or tk.get("id")
    cfg = common.load_config() if cfg is None else cfg
    card = brief.jira_card(key) if card is None else card
    b = tk.get("brief") or {}
    saved = b.get("summary") or {}
    model = {"unknowns": ((saved.get("sections") or {}).get("unknowns") or {}).get("claims") or [],
             "next_action": saved.get("next_action")}
    data = _assemble(tk,key,cfg,card,sections if sections is not None else b.get("sections") or {},model)
    rows = []
    for sid in ORDER:
        row = data["sections"][sid]
        rows.append(dict(row,id=sid,heading=HEADINGS[sid],empty=not row["claims"],
                         text=None if row["claims"] else "Nothing verified yet" if sid == "verified" else "Not recorded yet"))
    return {"sections":rows,"next_action":data["next_action"]}
