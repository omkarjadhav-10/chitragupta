"""Evidence Graph builder: turns records Chitragupta already keeps into graph rows. Pure over its arguments:
no file, no clock, no process, no network.

This part versions the requirements read from the cached Jira card (design section 9).
"""
import copy
import difflib
import re

from . import activity, gitcmd
from . import companionplanning as planning
from . import graphmodel
from .workflowmodel import digest

SIMILAR = planning.SIMILAR
MAX_SNAPSHOTS = 50
MAX_REVISIONS = 50      # per requirement: older wording keeps its digest and occurrences, not its text
MAX_REQUIREMENTS = 500
MAX_TEXT = 1000
NOT_REOBSERVED = "not re-observed; source partial"
TOO_MANY = "more than %d criteria" % MAX_REQUIREMENTS
EVENTS = ("added", "modified", "removed", "restored")


def _empty():
    return {"next_ordinal": 1, "source": {}, "logical": {}, "revisions": {}, "snapshots": []}


def text_fingerprint(digests):
    """Whether the requirements changed: the ordered row digests and nothing else (no summary, status or time)."""
    return digest(list(digests))[:16]


def _current(entry):
    return entry["history"][-1]["revision"]


def _source(rec, card, rows, gaps, proposed):
    failed = (rec or {}).get("error") or (card or {}).get("error")
    if (not rec and not card) or (card or {}).get("found") is False or (failed and not card):
        state = "inaccessible"
    else:
        state = "partial" if gaps else "current"
    # completeness: the Companion's own rule (companionplanning.propose)
    completeness = "unavailable" if state == "inaccessible" or not rows else "partial" if gaps else "complete"
    return {"state": state, "fetched_at": (rec or {}).get("fetched_at") or None,
            "error": planning.safe(failed, 200) if failed else None, "completeness": completeness,
            "gaps": [planning.safe(g, 300) for g in gaps], "comments_proposed": len(proposed)}


def _match(rows, logical):
    """Three whole passes → {row index: (requirement id, event or None)}."""
    taken, matched = set(), {}
    present = sorted((rid for rid, e in logical.items() if e["status"] != "removed"),
                     key=lambda rid: logical[rid]["ordinal"])
    everyone = sorted(logical, key=lambda rid: logical[rid]["ordinal"])

    def take(index, rid, event):
        matched[index] = (rid, event)
        taken.add(rid)

    # a1: the current wording of a requirement, exactly. Equal texts pair by source fragment, then by ordinal.
    for index, row in enumerate(rows):
        same = [rid for rid in present if rid not in taken and _current(logical[rid]) == row["digest"]]
        if same:
            here = (row["fragment"].get("field"), row["fragment"].get("index"))
            same.sort(key=lambda rid: ((logical[rid].get("fragment") or {}).get("field"),
                                       (logical[rid].get("fragment") or {}).get("index")) != here)
            take(index, same[0], None)
    # a2: an earlier wording, exactly: a removed requirement is back, or the text returned to what it was.
    for index, row in enumerate(rows):
        if index in matched:
            continue
        for rid in everyone:
            entry = logical[rid]
            if rid not in taken and any(h["revision"] == row["digest"] for h in entry["history"]):
                take(index, rid, "restored" if entry["status"] == "removed" else "modified")
                break
    # b: similar wording, best pair first. Every exact match is already taken, so rewording cannot steal one.
    pairs = sorted((-difflib.SequenceMatcher(None, planning.norm(row["text"]),
                                             planning.norm(logical[rid]["text"])).ratio(), index,
                    logical[rid]["ordinal"], rid)
                   for index, row in enumerate(rows) if index not in matched
                   for rid in present if rid not in taken)
    for ratio, index, _, rid in pairs:
        if -ratio >= SIMILAR and index not in matched and rid not in taken:
            take(index, rid, "modified")
    return matched


def requirement_snapshot(req_history, rec, card, fields, at, ticket, seq):
    """One read of the cached card against the `requirements` object of the history file.

    rec / card  `data/jira/<KEY>.json` and its card (either may be missing); fields: the configured criteria fields
    at          the time recorded when the card carries no `fetched_at`
    ticket      the ticket node id (part of each requirement's identity); seq: the journal sequence this run
                will publish under
    → (new requirements object, change). `req_history` is not modified. change = {added, modified, removed,
    restored: [gr ids], reordered, unseen, source: {from, to} or None, snapshot: bool, changed: bool}."""
    req = copy.deepcopy(req_history) if req_history else _empty()
    for name, value in _empty().items():
        req.setdefault(name, value)
    logical, revisions = req["logical"], req["revisions"]
    change = dict({name: [] for name in EVENTS}, reordered=False, unseen=[], source=None, snapshot=False)
    found, gaps, _, proposed = planning.extract(rec, card, fields)
    gaps = list(gaps)
    when = (rec or {}).get("fetched_at") or at

    rows = []
    for row in found:
        fragment = dict(row.get("fragment") or {})
        text = str(row["text"])[:MAX_TEXT]
        rows.append({"text": text, "source": row.get("source") or "", "fragment": fragment,
                     "digest": fragment.get("digest") or planning.fingerprint(text)})
    source = _source(rec, card, rows, gaps, proposed)
    matched = {}
    if source["state"] != "inaccessible":   # an inaccessible source says nothing about any requirement
        known = {rid: dict(e, text=(revisions.get(_current(e)) or {}).get("text") or "")
                 for rid, e in logical.items()}
        matched = _match(rows, known)
        new = [i for i in range(len(rows)) if i not in matched]
        over = len(logical) + len(new) - MAX_REQUIREMENTS
        if over > 0:   # further rows are a source gap, never a requirement
            cut = set(new[len(new) - over:])
            rows = [row for i, row in enumerate(rows) if i not in cut]
            gaps.append(TOO_MANY)
            source = _source(rec, card, rows, gaps, proposed)
            matched = _match(rows, known)

    previous = (req["source"] or {}).get("state")
    if previous != source["state"]:
        change["source"] = {"from": previous, "to": source["state"]}
    req["source"] = source
    if source["state"] == "inaccessible":
        change["changed"] = bool(change["source"])
        return req, change

    order = []
    for index, row in enumerate(rows):
        revision = revisions.setdefault(row["digest"], {"text": row["text"], "source": row["source"],
                                                        "fragment": row["fragment"], "first_seen": when})
        revision["last_seen"] = when
        if not revision.get("text"):   # wording whose text was dropped by the bound is back
            revision["text"] = row["text"]
            revision.pop("text_dropped", None)
        rid, event = matched.get(index, (None, "added"))
        if rid is None:
            same_root = sum(1 for e in logical.values() if e["root"] == row["digest"])
            rid = graphmodel.node_id("requirement", ticket, row["digest"], same_root)
            logical[rid] = {"ordinal": req["next_ordinal"], "root": row["digest"], "first_seen": when,
                            "status": "unconfirmed", "history": []}
            req["next_ordinal"] += 1
        entry = logical[rid]
        if event:
            entry["history"].append({"seq": seq, "at": when, "event": event, "revision": row["digest"]})
            change[event].append(rid)
            if event == "restored":
                entry["status"] = "unconfirmed"
        entry["fragment"] = {"field": row["fragment"].get("field"), "index": row["fragment"].get("index")}
        entry.pop("status_note", None)
        order.append(rid)

    seen = set(order)
    for rid in sorted(logical, key=lambda r: logical[r]["ordinal"]):
        entry = logical[rid]
        if rid in seen or entry["status"] == "removed":
            continue
        if source["completeness"] == "complete":
            entry["history"].append({"seq": seq, "at": when, "event": "removed", "revision": _current(entry)})
            entry["status"] = "removed"
            entry.pop("status_note", None)
            change["removed"].append(rid)
        else:   # no false absence: a partial read cannot remove a requirement
            entry["status_note"] = NOT_REOBSERVED
            change["unseen"].append(rid)

    fingerprint = text_fingerprint(row["digest"] for row in rows)
    last = req["snapshots"][-1] if req["snapshots"] else {"fingerprint": text_fingerprint([]), "order": []}
    events = any(change[name] for name in EVENTS)
    if not events:
        both = set(order) & set(last["order"])
        change["reordered"] = [r for r in order if r in both] != [r for r in last["order"] if r in both]
    if events or change["reordered"] or fingerprint != last["fingerprint"]:
        req["snapshots"].append(dict({name: list(change[name]) for name in EVENTS}, seq=seq, at=when,
                                     fingerprint=fingerprint, order=order, reordered=change["reordered"],
                                     unseen=list(change["unseen"])))
        req["snapshots"] = req["snapshots"][-MAX_SNAPSHOTS:]
        change["snapshot"] = True
    _drop_old_text(req)
    change["changed"] = bool(change["snapshot"] or change["source"])
    return req, change


def _drop_old_text(req):
    """Above the per-requirement revision bound the oldest wording loses its text; digests and occurrences stay."""
    logical, revisions = req["logical"], req["revisions"]
    current = {_current(e) for e in logical.values()}
    for entry in logical.values():
        seen = []
        for occurrence in entry["history"]:
            if occurrence["revision"] not in seen:
                seen.append(occurrence["revision"])
        for old in seen[:-MAX_REVISIONS] if len(seen) > MAX_REVISIONS else []:
            if old not in current and revisions.get(old, {}).get("text"):
                revisions[old]["text"] = ""
                revisions[old]["text_dropped"] = True


# --- Companion workflows: baselines, plans and plan coverage (design sections 9.7, 10 "Plans", 15) ---

WORKFLOW_FIELDS = ("id", "stage", "archived", "baseline", "baselines", "plans", "selected_plan", "approved_plan",
                   "approvals", "evaluations", "evidence", "observations", "context", "graph_checkpoint")
BINDING_FIELDS = ("ticket", "jira_instance", "repo_id", "location", "profile_generation")
LOCAL = "local baseline"
BASELINES_DIFFER = "baselines differ"
NOT_APPROVED = "selected, not approved"


def workflow_inputs(rec):
    """The one documented list of workflow-record fields the graph depends on. Nothing else is read."""
    from . import companion
    out = {name: copy.deepcopy(rec.get(name)) for name in WORKFLOW_FIELDS}
    out["binding"] = {name: (rec.get("binding") or {}).get(name) for name in BINDING_FIELDS}
    out["plan_order"] = companion.plan_order(rec)
    return out


def _criterion_digest(criterion):
    return (criterion.get("digest") or (criterion.get("fragment") or {}).get("digest")
            or planning.fingerprint(criterion["text"]))


def revision_id(requirement, revision):
    return graphmodel.node_id("requirement_revision", requirement, revision)


def baseline_links(req_history, workflows, ticket):
    """Match baseline criteria to requirement revisions by digest. → the requirement index:

    requirements  {gr: {status, revision, ordinal, source, text, aliases: {workflow: acN},
                        confirmed: {workflow: revision}, ambiguity}}
    baselines     {workflow: {baseline: {acN: {requirement, revision, excluded, rationale, weight}}}}
    confirmed_in  {"<gr>|<revision>": [{workflow, baseline, current}]}

    Only a workflow's CURRENT baseline gives aliases and confirmation, and only it can introduce a requirement
    Jira never showed (`source: "local baseline"`). Earlier baselines add `confirmed_in` history and nothing else."""
    logical = (req_history or {}).get("logical") or {}
    texts = (req_history or {}).get("revisions") or {}
    reqs = {}
    for rid, entry in logical.items():
        current = _current(entry)
        reqs[rid] = {"status": "removed" if entry["status"] == "removed" else "unconfirmed", "revision": current,
                     "ordinal": entry["ordinal"], "source": "jira", "text": (texts.get(current) or {}).get("text") or "",
                     "aliases": {}, "confirmed": {}, "ambiguity": None}
    index = {"ticket": ticket, "requirements": reqs, "baselines": {}, "confirmed_in": {}}

    def observed(wanted, criterion, used):
        """The requirement that showed this wording: present before removed, current wording before earlier,
        the same source fragment before another, then by ordinal."""
        here = ((criterion.get("fragment") or {}).get("field"), (criterion.get("fragment") or {}).get("index"))
        found = [rid for rid, entry in logical.items()
                 if rid not in used and any(h["revision"] == wanted for h in entry["history"])]
        found.sort(key=lambda rid: (logical[rid]["status"] == "removed", _current(logical[rid]) != wanted,
                                    ((logical[rid].get("fragment") or {}).get("field"),
                                     (logical[rid].get("fragment") or {}).get("index")) != here,
                                    logical[rid]["ordinal"]))
        return found[0] if found else None

    def link(workflow, bid, is_current):
        wid, base = workflow["id"], workflow["baselines"][bid]
        used, local_seen, mapping = set(), {}, {}
        for criterion in base.get("criteria") or []:
            wanted = _criterion_digest(criterion)
            rid = observed(wanted, criterion, used)
            if rid is None:   # wording Jira never showed: a clarification, an accepted comment, a split
                nth = local_seen.get(wanted, 0)
                local_seen[wanted] = nth + 1
                rid = graphmodel.node_id("requirement", ticket, LOCAL, wanted, nth)
                if is_current:
                    reqs.setdefault(rid, {"status": "current", "revision": wanted, "ordinal": None, "source": LOCAL,
                                          "text": planning.safe(criterion.get("text"), MAX_TEXT), "aliases": {},
                                          "confirmed": {}, "ambiguity": None})
                elif rid not in reqs:
                    continue
            used.add(rid)
            mapping[criterion["id"]] = {"requirement": rid, "revision": wanted,
                                        "excluded": bool(criterion.get("excluded")),
                                        "rationale": planning.safe(criterion.get("rationale"), 300),
                                        "weight": criterion.get("weight", 1)}
            index["confirmed_in"].setdefault("%s|%s" % (rid, wanted), []).append(
                {"workflow": wid, "baseline": bid, "current": is_current})
            if is_current:
                reqs[rid]["aliases"][wid] = criterion["id"]
                reqs[rid]["confirmed"][wid] = wanted
                if reqs[rid]["status"] == "unconfirmed":
                    reqs[rid]["status"] = "current"
        index["baselines"].setdefault(wid, {})[bid] = mapping

    ordered = sorted(workflows, key=lambda w: w["id"])
    for workflow in ordered:   # every current baseline first, so a local requirement exists before history names it
        if workflow.get("baseline") in (workflow.get("baselines") or {}):
            link(workflow, workflow["baseline"], True)
    for workflow in ordered:
        for bid in sorted(workflow.get("baselines") or {}):
            if bid != workflow.get("baseline"):
                link(workflow, bid, False)
    for entry in reqs.values():
        if len(set(entry["confirmed"].values())) > 1:
            entry["ambiguity"] = BASELINES_DIFFER
    return index


def judged_plan(workflow):
    """→ (plan id or None, label): the plan a workflow is judged by is its approved plan, else its selected one."""
    plans = workflow.get("plans") or {}
    if workflow.get("approved_plan") in plans:
        return workflow["approved_plan"], "approved"
    if workflow.get("selected_plan") in plans:
        return workflow["selected_plan"], NOT_APPROVED
    return None, "no plan"


def _evaluations(workflow, plan_id):
    rows = [e for e in (workflow.get("evaluations") or {}).values() if isinstance(e, dict) and e.get("plan") == plan_id]
    return sorted(rows, key=lambda e: (str(e.get("at") or ""), str(e.get("id") or "")))


def plan_nodes(workflow, requirement_index):
    """Plans, their steps and what they address, for one workflow. → {nodes, edges, judged, label}.

    Every saved plan stays: versions before the one the workflow is judged by are `superseded`, and so are their
    step mappings, each scoped to its own plan node. A step id means something only together with that node."""
    wid, binding = workflow["id"], workflow.get("binding") or {}
    ticket, plans = binding.get("ticket"), workflow.get("plans") or {}
    order = [pid for pid in workflow.get("plan_order") or sorted(plans) if pid in plans]
    judged, label = judged_plan(workflow)
    pivot = order.index(judged) if judged in order else len(order) - 1
    place = {"workflow": wid, "repo_id": binding.get("repo_id") or "", "location": binding.get("location") or ""}
    pid_node = {pid: graphmodel.node_id("plan", wid, pid) for pid in order}
    mapped = (requirement_index.get("baselines") or {}).get(wid) or {}
    nodes, edges = [], []
    for number, pid in enumerate(order, 1):
        plan, pnode = plans[pid], pid_node[pid]
        life = "superseded" if number - 1 < pivot else "current"
        approved, selected = pid == workflow.get("approved_plan"), pid == workflow.get("selected_plan")
        evaluations = _evaluations(workflow, pid)
        state = ("approved" if approved else NOT_APPROVED if selected and pid == judged
                 else "superseded" if life == "superseded" else "saved")
        ref = "workflow:%s/plan:%s" % (wid, pid)
        nodes.append(graphmodel.node(
            "plan", (wid, pid), ticket, "Plan v%d" % number,
            {"version": number, "source": plan.get("source"), "baseline": plan.get("baseline"), "approved": approved,
             "selected": selected, "judged": pid == judged, "state": state,
             "parent": pid_node.get(plan.get("parent")), "evaluation": evaluations[-1].get("id") if evaluations else None},
            lifecycle=life, source_ref=ref, **place))
        if plan.get("parent") in pid_node:
            edges.append(graphmodel.edge("SUPERSEDES", pnode, pid_node[plan["parent"]], ticket, "observed",
                                         evidence_ref=ref, attrs={"how": "parent"}, workflow=wid))
        step_node = {}
        criteria = mapped.get(plan.get("baseline")) or {}
        for step in plan.get("steps") or []:
            snode = step_node[step["id"]] = graphmodel.node_id("plan_step", pnode, step["id"])
            nodes.append(graphmodel.node("plan_step", (pnode, step["id"]), ticket, step.get("text"),
                                         {"step": step["id"], "kind": step.get("kind"), "plan": pnode},
                                         lifecycle=life, source_ref=ref, **place))
            for dep in step.get("depends_on") or []:
                if dep in step_node:
                    edges.append(graphmodel.edge("DEPENDS_ON", snode, step_node[dep], ticket, "declared", scope=pnode,
                                                 lifecycle=life, workflow=wid))
            for cid in step.get("criteria") or []:
                if cid in criteria:   # the revision this plan's own baseline confirmed
                    edges.append(graphmodel.edge(
                        "ADDRESSED_BY", revision_id(criteria[cid]["requirement"], criteria[cid]["revision"]), snode,
                        ticket, "declared", scope=pnode, evidence_ref=ref,
                        attrs={"approved": approved, "criterion": cid}, lifecycle=life, workflow=wid))
    previous = None
    for approval in workflow.get("approvals") or []:   # approval succession, one edge per recorded occurrence
        pid = (approval or {}).get("plan")
        if pid not in pid_node or pid == previous:
            continue
        if previous:
            edges.append(graphmodel.edge("SUPERSEDES", pid_node[pid], pid_node[previous], ticket, "observed",
                                         scope=str(approval.get("id") or ""), observed_at=str(approval.get("at") or ""),
                                         evidence_ref="workflow:%s/approval:%s" % (wid, approval.get("id") or ""),
                                         attrs={"how": "approval"}, workflow=wid))
        previous = pid
    unique = {}
    for row in edges:
        unique.setdefault(row["id"], row)
    return {"nodes": nodes, "edges": sorted(unique.values(), key=lambda row: row["id"]), "judged": judged,
            "label": label}


def plan_coverage_rows(workflow, requirement_index):
    """Plan coverage per requirement: for one workflow (the requirements its current baseline confirms), or with
    `workflow` None the ticket-level rows, where there is no plan to judge by.
    → {gr: {state, basis, reason, plan, judged, alias, excluded, rationale}}"""
    reqs = requirement_index.get("requirements") or {}
    if workflow is None:
        return {rid: dict(graphmodel.plan_coverage(False, False, None, False), plan=None, judged="no plan", alias=None,
                          excluded=False, rationale="") for rid in reqs}
    wid = workflow["id"]
    baselines = (requirement_index.get("baselines") or {}).get(wid) or {}
    current = baselines.get(workflow.get("baseline")) or {}
    judged, label = judged_plan(workflow)
    plan = (workflow.get("plans") or {}).get(judged) or {}
    planned = baselines.get(plan.get("baseline")) or {}
    by_requirement = {entry["requirement"]: (cid, entry) for cid, entry in planned.items()}
    out = {}
    for alias, entry in current.items():
        rid = entry["requirement"]
        cid, seen = by_requirement.get(rid, (None, None))
        answer = None
        if cid is not None:
            for evaluation in reversed(_evaluations(workflow, judged)):   # the newest that classifies it
                found = (evaluation.get("answers") or {}).get(cid)
                if evaluation.get("state", "available") == "available" and isinstance(found, dict) \
                        and found.get("status") in graphmodel.ORDER["plan_coverage"]:
                    developer = evaluation.get("origin") == graphmodel.DEVELOPER_ORIGIN
                    answer = {"state": found["status"], "confidence": found.get("confidence"),
                              "origin": graphmodel.DEVELOPER_ORIGIN if developer else graphmodel.JEV_ORIGIN}
                    break
        status = graphmodel.plan_coverage(
            judged is not None, seen is None or seen["revision"] == reqs[rid]["revision"], answer,
            cid is not None and any(cid in (step.get("criteria") or []) for step in plan.get("steps") or []))
        out[rid] = dict(status, plan=graphmodel.node_id("plan", wid, judged) if judged else None, judged=label,
                        alias=alias, excluded=entry["excluded"], rationale=entry["rationale"])
    return out


# --- repositories, commits and code changes (design sections 5 and 10 "Code and commits") ---
# Everything comes from records the tick already wrote. No Git command, no process, no file read.

UNKNOWN_LOCATION = "unknown-location"
MAX_FILES = 200


def _place(path):
    """A location as a comparison key (no filesystem access): slashes one way, no trailing one, drive paths folded."""
    text = str(path or "").replace("\\", "/").rstrip("/")
    return text.lower() if re.match(r"^[A-Za-z]:", text) else text


def repo_identity(repo_key, tk, ledgers, bindings, repo_json=None):
    """The canonical identity of the repository a ticket record calls `repo_key` (a folder name).

    ledgers    activity ledgers already loaded (`{repo: {id, path}}`); bindings: workflow bindings
               (`{repo_id, location}`); repo_json: `data/git/<repo_key>/repo.json`
    Order: a ledger at a location this ticket recorded → a workflow binding there → `repo.json` adapted (its
    `path` is the local folder, so only `remote_path` and the host fields are used) → the folder name.
    → {id, identity_basis, locations: {location: id}} plus `location` when exactly one is known and `candidates`
    when the sources disagree and nothing ties the ticket to one of them (`identity_basis: "ambiguous"`)."""
    def here(path):
        return bool(path) and gitcmd.repo_key(str(path)) == repo_key

    recorded = {_place(f.get("repo_root")) for f in ((tk or {}).get("files") or {}).values()
                if isinstance(f, dict) and here(f.get("repo_root"))}
    recorded |= {_place(b.get("location")) for b in bindings or [] if here((b or {}).get("location"))}
    found, locations = [], {}   # (basis, id, tied to a recorded location)
    for led in ledgers or []:
        repo = (led or {}).get("repo") or {}
        if repo.get("id") and here(repo.get("path")):
            found.append(("ledger", repo["id"], _place(repo["path"]) in recorded))
            locations[_place(repo["path"])] = repo["id"]
    for binding in bindings or []:
        if (binding or {}).get("repo_id") and here(binding.get("location")):
            found.append(("binding", binding["repo_id"], True))
            locations.setdefault(_place(binding["location"]), binding["repo_id"])
    meta = repo_json or {}
    if meta.get("host") in ("github", "gitlab", "azure"):
        info = {k: meta[k] for k in ("host", "org", "project", "repo", "instance") if meta.get(k)}
        if meta.get("remote_path"):
            info["path"] = meta["remote_path"]
        adapted = activity.canonical_id(info, "")
        if not adapted.startswith("path:"):
            found.append(("repo.json", adapted, False))
    out = {"locations": locations}
    if len(locations) == 1:
        out["location"] = next(iter(locations))
    tied = [row for row in found if row[2]]
    for rows in (tied, found):   # what the ticket is tied to decides first; then every source, if they agree
        ids = sorted({row[1] for row in rows})
        if len(ids) == 1:
            return dict(out, id=ids[0], identity_basis=next(row[0] for row in rows if row[1] == ids[0]))
        if rows is tied and ids:
            break
    if not found:
        return dict(out, id="name:" + repo_key, identity_basis="folder-name")
    return dict(out, id="name:" + repo_key, identity_basis="ambiguous", candidates=sorted({row[1] for row in found}))


def _how(key, commit):
    if key and key in str(commit.get("subject") or ""):
        return "commit message"
    if key and key in str(commit.get("ref") or ""):
        return "branch name"
    return "recorded attribution"


def commit_nodes(tk, identities, ticket):
    """Commits, code changes and the relationships between commits, from one ticket record.

    identities  {repo_key: repo_identity(...)}; a repository without one gets the folder-name identity
    ticket      the ticket node id
    → {nodes, edges}. Ticket-level evidence only: nothing here reaches a requirement."""
    key = (tk or {}).get("id") or ""
    nodes, edges = {}, {}

    def identity(name):
        rkey = gitcmd.repo_key(str(name or ""))
        return rkey, identities.get(rkey) or {"id": "name:" + rkey, "identity_basis": "folder-name", "locations": {}}

    def put(row, into):
        into.setdefault(row["id"], row)
        return row["id"]

    def weak(ident):
        extra = {"identity_basis": ident["identity_basis"]}
        if ident.get("candidates"):
            extra["candidates"] = ident["candidates"]
        return extra

    def commit(repo, sha, mine, subject="", date="", ref="", complete=True):
        rkey, ident = identity(repo)
        attrs = dict({"sha": sha, "short": sha[:9], "date": date, "mine": mine, "ref": ref,
                      "sha_basis": "full" if complete else "prefix"}, **weak(ident))
        return put(graphmodel.node("commit", (ident["id"], sha), key, subject or sha[:9], attrs, repo_id=ident["id"],
                                   observed_at=str(date or ""), source_ref="ticket:%s/commit:%s" % (key, sha)), nodes)

    def evidence(dst, how, ref):
        put(graphmodel.edge("HAS_EVIDENCE", ticket, dst, key, "observed", evidence_ref=ref, attrs={"how": how}), edges)

    full = {}   # repo_key -> full shas this record knows
    commits = (tk or {}).get("commits") or {}
    for sha in sorted(commits):
        c = commits[sha]
        rkey, ident = identity(c.get("repo_name"))
        full.setdefault(rkey, set()).add(sha)
        node = commit(rkey, sha, True, c.get("subject"), c.get("date"), c.get("ref"))
        evidence(node, _how(key, c), "ticket:%s/commit:%s" % (key, sha))
        for path in sorted(set(c.get("files") or []))[:MAX_FILES]:
            change = put(graphmodel.node("code_change", (node, path), key, path,
                                         {"path": path, "state": "committed", "verified": True}, repo_id=ident["id"],
                                         observed_at=str(c.get("date") or ""),
                                         source_ref="ticket:%s/commit:%s" % (key, sha)), nodes)
            put(graphmodel.edge("PART_OF_COMMIT", change, node, key, "observed",
                                evidence_ref="ticket:%s/commit:%s" % (key, sha)), edges)

    files = (tk or {}).get("files") or {}
    for name in sorted(files):
        f = files[name]
        if not isinstance(f, dict) or not f.get("changed") or f.get("committed") or f.get("verified_by") != "edit":
            continue
        path = f.get("path") or name
        rkey, ident = identity(f.get("repo"))
        location = _place(f.get("repo_root")) or UNKNOWN_LOCATION
        repo_id = ident["locations"].get(location) or ident["id"]
        change = put(graphmodel.node("code_change", (repo_id, location, path, "uncommitted"), key, path,
                                     dict({"path": path, "state": "uncommitted", "verified": bool(f.get("verified"))},
                                          **weak(ident)),
                                     repo_id=repo_id, location="" if location == UNKNOWN_LOCATION else location,
                                     observed_at=str(f.get("last") or ""), source_ref="ticket:%s/file:%s" % (key, path)),
                     nodes)
        evidence(change, "session edit", "ticket:%s/file:%s" % (key, path))

    repos = ((tk or {}).get("git") or {}).get("repos") or {}
    for name in sorted(repos):
        repo = repos[name] or {}
        rkey, _ = identity(name)
        known = set(full.get(rkey) or ())
        prs = {p.get("number"): p for p in repo.get("prs") or [] if isinstance(p, dict)}
        for p in prs.values():
            known.update(s for s in (p.get("merge_commit"), p.get("head_at_merge"), p.get("head")) if s)

        def whole(sha):
            """The full sha of a recorded prefix when exactly one known commit starts with it."""
            hits = [s for s in known if s.startswith(sha)]
            return (hits[0], True) if len(hits) == 1 else (sha, len(sha) >= 40)

        for row in repo.get("rows") or []:
            ref = "delivery:%s/%s" % (rkey, row.get("id") or "")
            ids = []
            for sha in list(row.get("shas") or []) + list(row.get("loose") or []):
                sha, complete = whole(str(sha))
                node = graphmodel.node_id("commit", identity(rkey)[1]["id"], sha)
                if node not in nodes:
                    commit(rkey, sha, "unknown", row.get("subject"), row.get("date"), complete=complete)
                    evidence(node, "delivery row", ref)
                ids.append(node)
            same, loose = ids[:len(row.get("shas") or [])], ids[len(row.get("shas") or []):]
            for other in same[1:]:   # the same patch on another branch
                put(graphmodel.edge("EQUIVALENT_TO", other, same[0], key, "observed", evidence_ref=ref,
                                    attrs={"patch": "same"}), edges)
            for copy_ in loose:      # a -x copy whose patch differs: lineage, not equivalence
                if same:
                    put(graphmodel.edge("CHERRY_PICKED_FROM", copy_, same[0], key, "observed", evidence_ref=ref,
                                        attrs={"patch": "changed"}), edges)
            for cell in (row.get("cells") or {}).values():   # the delivery check found this row inside a squash merge
                p = prs.get((cell or {}).get("pr")) if (cell or {}).get("s") == "squash" else None
                if p and p.get("state") == "merged" and p.get("merge_commit") and p.get("head_at_merge"):
                    merge = commit(rkey, p["merge_commit"], "unknown", "Squash merge of pull request #%s" % p["number"],
                                   p.get("merged_at"))
                    for node in same:
                        if node != merge:
                            put(graphmodel.edge("SQUASHED_INTO", node, merge, key, "observed", evidence_ref=ref,
                                                attrs={"pr": p["number"]}), edges)
    return {"nodes": sorted(nodes.values(), key=lambda row: row["id"]),
            "edges": sorted(edges.values(), key=lambda row: row["id"])}
