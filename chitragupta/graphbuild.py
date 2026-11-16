"""Evidence Graph builder: turns records Chitragupta already keeps into graph rows. Pure over its arguments:
no file, no clock, no process, no network.

This part versions the requirements read from the cached Jira card (design section 9).
"""
import copy
import difflib
import re

from . import activity, gitcmd, hosts, profile
from . import companionevidence as evidence_rows
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
    provider, instance = profile.host_instance(meta)
    out = {"repo_key": repo_key, "locations": locations, "provider": provider or "", "instance": instance or "",
           "tracked": _place(meta.get("path"))}   # the clone whose branches and refs `data/git/<repo_key>` describes
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


def _identity(identities, name):
    """(repo_key, identity) of a repository a record names by its folder; unknown ones say so."""
    rkey = gitcmd.repo_key(str(name or ""))
    return rkey, (identities or {}).get(rkey) or {"id": "name:" + rkey, "identity_basis": "folder-name",
                                                  "locations": {}, "repo_key": rkey, "tracked": ""}


def _put(row, into):
    into.setdefault(row["id"], row)
    return row["id"]


def _weak(ident):
    extra = {"identity_basis": ident["identity_basis"]}
    if ident.get("candidates"):
        extra["candidates"] = ident["candidates"]
    return extra


def _commit(key, ident, sha, mine, subject="", date="", ref="", complete=True):
    attrs = dict({"sha": sha, "short": sha[:9], "date": date, "mine": mine, "ref": ref,
                  "sha_basis": "full" if complete else "prefix"}, **_weak(ident))
    return graphmodel.node("commit", (ident["id"], sha), key, subject or sha[:9], attrs, repo_id=ident["id"],
                           observed_at=str(date or ""), source_ref="ticket:%s/commit:%s" % (key, sha))


def _known_shas(tk, rkey, repo):
    """Every full commit id this record holds for one repository: own commits and the PRs' heads and merges."""
    known = {sha for sha, c in ((tk or {}).get("commits") or {}).items()
             if gitcmd.repo_key(str((c or {}).get("repo_name") or "")) == rkey}
    for p in (repo or {}).get("prs") or []:
        if isinstance(p, dict):
            known.update(s for s in (p.get("merge_commit"), p.get("head_at_merge"), p.get("head")) if s)
    return known


def _whole(known, sha):
    """(sha, complete): the full id of a recorded prefix when exactly one known commit starts with it."""
    sha = str(sha or "")
    hits = [s for s in known if s.startswith(sha)] if sha else []
    return (hits[0], True) if len(hits) == 1 else (sha, len(sha) >= 40)


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
        return _identity(identities, name)

    put, weak = _put, _weak

    def commit(repo, sha, mine, subject="", date="", ref="", complete=True):
        return put(_commit(key, identity(repo)[1], sha, mine, subject, date, ref, complete), nodes)

    def evidence(dst, how, ref):
        put(graphmodel.edge("HAS_EVIDENCE", ticket, dst, key, "observed", evidence_ref=ref, attrs={"how": how}), edges)

    commits = (tk or {}).get("commits") or {}
    for sha in sorted(commits):
        c = commits[sha]
        rkey, ident = identity(c.get("repo_name"))
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
        known = _known_shas(tk, rkey, repo)
        prs = {p.get("number"): p for p in repo.get("prs") or [] if isinstance(p, dict)}

        def whole(sha):
            return _whole(known, sha)

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


# --- pull requests, CI and release evidence (design section 10). Read from the record; no provider is called. ---

_PROVIDER = {"gh": "github", "glab": "gitlab", "az": "azure"}
STAGES = ("release", "qa", "uat")


def pull_request_id(ident, number, src=""):
    """A pull request is (provider, instance, canonical repository, number): never the number alone."""
    return graphmodel.node_id("pull_request", ident.get("provider") or _PROVIDER.get(src) or "",
                              ident.get("instance") or "", ident["id"], number)


def _sorted(nodes, edges):
    return {"nodes": sorted(nodes.values(), key=lambda row: row["id"]),
            "edges": sorted(edges.values(), key=lambda row: row["id"])}


def pr_nodes(tk, identities, ticket, commits=()):
    """Pull requests of the ticket's branches, shared integration PRs, and the CI runs of each PR head.

    commits  the nodes `commit_nodes` returned: `INCLUDED_IN_PR` is drawn only to commits that exist
    → {nodes, edges}"""
    key = (tk or {}).get("id") or ""
    have = {row["id"] for row in commits or () if row.get("type") == "commit"}
    nodes, edges = {}, {}
    repos = ((tk or {}).get("git") or {}).get("repos") or {}
    release = ((tk or {}).get("release") or {}).get("repos") or {}
    for name in sorted(set(repos) | set(release)):
        repo = repos.get(name) or {}
        rkey, ident = _identity(identities, name)
        known = _known_shas(tk, rkey, repo)
        own = set()
        for p in repo.get("prs") or []:
            if not isinstance(p, dict) or p.get("number") is None:
                continue
            own.add(p["number"])
            p = evidence_rows.with_ci(repo, p)
            head = str(p.get("head") or "").lower()
            ci = hosts.ci_state(p)
            ref = "git:%s:pr:%s" % (rkey, p["number"])
            node = _put(graphmodel.node(
                "pull_request", (ident.get("provider") or _PROVIDER.get(p.get("src")) or "", ident.get("instance") or "",
                                 ident["id"], p["number"]), key, "#%s %s" % (p["number"], p.get("title") or ""),
                dict({"number": p["number"], "state": p.get("state"), "review": p.get("review") or "", "head": head,
                      "source": p.get("source"), "target": p.get("target"), "shared": False,
                      "ci": {"state": ci["state"], "head": ci["head"], "coverage": ci["coverage"],
                             "stale": ci["stale"], "checks": ci["checks"], "observed_at": ci["observed_at"],
                             "reason": ci["reason"]}}, **_weak(ident)),
                repo_id=ident["id"], observed_at=str(p.get("merged_at") or p.get("created_at") or ""), source_ref=ref),
                nodes)
            _put(graphmodel.edge("HAS_EVIDENCE", ticket, node, key, "observed", evidence_ref=ref,
                                 attrs={"how": "branch link"}), edges)
            branch = next((b for b in repo.get("branches") or [] if isinstance(b, dict)
                           and b.get("branch") == p.get("source")), {})
            for c in branch.get("commits") or []:   # the PR source branch's own commits
                sha, _ = _whole(known, c.get("sha"))
                commit = graphmodel.node_id("commit", ident["id"], sha)
                if commit in have:
                    _put(graphmodel.edge("INCLUDED_IN_PR", commit, node, key, "observed", evidence_ref=ref,
                                         attrs={"at_head": bool(sha) and head.startswith(sha.lower())}), edges)
            if p.get("state") == "merged" and p.get("merge_commit"):
                merge = graphmodel.node_id("commit", ident["id"], p["merge_commit"])
                if merge not in have:
                    _put(_commit(key, ident, p["merge_commit"], "unknown", "Merge of pull request #%s" % p["number"],
                                 p.get("merged_at")), nodes)
                _put(graphmodel.edge("INCLUDED_IN_PR", merge, node, key, "observed", evidence_ref=ref,
                                     attrs={"at_head": False, "merge": True}), edges)
            for row, _ in evidence_rows.ci_rows(rkey, p):   # one per run attempt, on the head it ran for
                current = row["head"] == head
                life = "current" if current else "historical"
                run_id = row["id"].rsplit(":", 2)[1]
                case = _put(graphmodel.node("test_case", (row["provider"], ident["id"], row["check"]), key, row["check"],
                                            {"test_type": "ci-check", "app": row["app"]}, repo_id=ident["id"],
                                            source_ref=ref), nodes)
                run = _put(graphmodel.node(
                    "test_run", (row["provider"], ident["id"], run_id, row["attempt"]), key, row["label"],
                    {"status": row["outcome"], "source": "ci", "head": row["head"], "attempt": row["attempt"],
                     "pr": p["number"], "evidence": row["id"], "context": sorted(row["context"]),
                     "applicability": "current head" if current else "earlier head"},
                    lifecycle=life, repo_id=ident["id"], observed_at=str(row["observed_at"] or ""),
                    source_ref=row["source_ref"]), nodes)
                _put(graphmodel.edge("EXECUTED_IN", case, run, key, "observed", evidence_ref=row["source_ref"],
                                     lifecycle=life), edges)
                _put(graphmodel.edge("CHECKED_BY", node, run, key, "observed", scope=row["head"],
                                     evidence_ref=row["source_ref"], lifecycle=life), edges)
        for p in (release.get(name) or {}).get("shared") or []:
            if not isinstance(p, dict) or p.get("number") is None or p["number"] in own:
                continue   # carried by someone else's PR: linked to the ticket, never credited to it
            ref = "release:%s:shared:%s" % (rkey, p["number"])
            node = _put(graphmodel.node(
                "pull_request", (ident.get("provider") or _PROVIDER.get(p.get("src")) or "", ident.get("instance") or "",
                                 ident["id"], p["number"]), key, "#%s %s" % (p["number"], p.get("title") or ""),
                dict({"number": p["number"], "state": p.get("state"), "review": "", "head": "",
                      "source": p.get("source"), "target": p.get("target"), "shared": True,
                      "carries": p.get("carries"), "of": p.get("of"), "role": p.get("role"),
                      "ci": {"state": "unavailable", "reason": "not read for shared pull requests"}}, **_weak(ident)),
                repo_id=ident["id"], observed_at=str(p.get("merged_at") or p.get("created_at") or ""), source_ref=ref),
                nodes)
            _put(graphmodel.edge("HAS_EVIDENCE", ticket, node, key, "observed", evidence_ref=ref,
                                 attrs={"how": "shared integration pull request"}), edges)
    return _sorted(nodes, edges)


def release_nodes(tk, identities, ticket, commits=()):
    """Release evidence, one node per fact and never merged: inclusion per stage and repository, UAT deployment,
    UAT acceptance. States are taken as `release` decided them (a stale or unpinned stage is already `unknown`).
    A commit is linked to an inclusion only through its own delivery row (the same patch), never through a
    cherry-pick whose patch changed. → {nodes, edges}"""
    key = (tk or {}).get("id") or ""
    have = {row["id"] for row in commits or () if row.get("type") == "commit"}
    nodes, edges = {}, {}
    rel = (tk or {}).get("release") or {}
    repos = ((tk or {}).get("git") or {}).get("repos") or {}

    def fact(ident, kind, bound, label, attrs, ref, observed="", life="current"):
        node = _put(graphmodel.node("release_evidence", (ticket, ident, kind, bound), key, label, attrs, repo_id=ident,
                                    observed_at=str(observed or ""), source_ref=ref, lifecycle=life), nodes)
        _put(graphmodel.edge("HAS_RELEASE_EVIDENCE", ticket, node, key, "observed", evidence_ref=ref,
                             lifecycle=life), edges)
        return node

    for name in sorted(rel.get("repos") or {}):
        entry = rel["repos"][name] or {}
        if entry.get("candidate"):
            continue
        rkey, ident = _identity(identities, name)
        stale = bool((entry.get("freshness") or {}).get("stale"))
        known = _known_shas(tk, rkey, repos.get(name) or {})
        for role in STAGES:
            stage = (entry.get("stages") or {}).get(role)
            if not isinstance(stage, dict):
                continue
            ref = "release:%s:stage:%s" % (rkey, role)
            node = fact(ident["id"], "inclusion", role, "%s: %s" % (role, stage.get("state") or "unknown"),
                        dict({"fact": "inclusion", "environment": role, "state": stage.get("state") or "unknown",
                              "target": stage.get("target"), "counts": stage.get("counts") or {},
                              "detail": stage.get("detail") or "", "freshness": "stale" if stale else "fresh"},
                             **_weak(ident)), ref, (entry.get("complete_at") or {}).get(role))
            for row in (repos.get(name) or {}).get("rows") or []:
                cell = (row.get("cells") or {}).get(role)
                if not isinstance(cell, dict) or not cell.get("s"):
                    continue
                for sha in row.get("shas") or []:   # the row's patch-equal commits only; `loose` copies never
                    commit = graphmodel.node_id("commit", ident["id"], _whole(known, sha)[0])
                    if commit in have:
                        _put(graphmodel.edge("HAS_RELEASE_EVIDENCE", commit, node, key, "observed", evidence_ref=ref,
                                             attrs={"state": cell["s"]}), edges)
    uat = rel.get("uat") if isinstance(rel.get("uat"), dict) else {}
    for kind, name in (("deployed", "deployment"), ("accepted", "acceptance")):
        found = uat.get(kind)
        if isinstance(found, dict):
            state = "yes" if found.get("state") == "yes" else "unknown"
            fact("", name, found.get("rev") or "", "UAT %s: %s" % (name, state),
                 {"fact": name, "environment": "uat", "state": state, "revision": found.get("rev"),
                  "source": found.get("source") or "", "carried": bool(found.get("carried"))},
                 "release:uat:%s" % kind, found.get("at"))
        for old in uat.get("history") or []:   # what was confirmed for an earlier UAT revision stays history
            if isinstance(old, dict) and old.get("kind") == kind and old.get("rev") \
                    and old["rev"] != (found or {}).get("rev"):
                fact("", name, old["rev"], "UAT %s: yes (earlier revision)" % name,
                     {"fact": name, "environment": "uat", "state": "yes", "revision": old["rev"],
                      "source": old.get("source") or "", "carried": False},
                     "release:uat:%s:%s" % (kind, old["rev"]), old.get("at"), "historical")
    return _sorted(nodes, edges)


# --- local test runs and their applicability (design section 10 "Tests") ---
# A run is compared with named references only (a PR head, a work-branch tip). There is no global "current
# revision", and nothing here verifies a requirement.

MIN_HEAD = 7
_HEX = re.compile(r"^[0-9a-f]+$")
NOT_CAPTURED = "revision not captured"
NOTHING_TO_COMPARE = "nothing to compare with"
RUN_STATES = ("passed", "failed", "interrupted", "running", "unknown")


def same_head(a, b):
    """True / False when two commit ids can be compared, None when they cannot: each must be at least 7 hex
    characters, and they are the same commit only when one is a prefix of the other."""
    a, b = str(a or "").lower(), str(b or "").lower()
    if min(len(a), len(b)) < MIN_HEAD or not _HEX.match(a) or not _HEX.match(b):
        return None
    return a.startswith(b) or b.startswith(a)


def references(tk, identity, location, branches=None):
    """What a local run in `location` may be compared with, for one repository: the heads of its open pull
    requests, and the tips of the ticket's work branches when `location` is the clone those tips were read from.
    → [{name, kind, head, observed_at}], sorted by name.

    branches  `data/git/<repo_key>/branches.json` (branch → {tip, checked_at, …}) when the caller has it; without
              it a branch tip is the newest own commit the ticket record lists (a 9-character prefix)."""
    repo = (((tk or {}).get("git") or {}).get("repos") or {}).get((identity or {}).get("repo_key")) or {}
    checked = str(((tk or {}).get("git") or {}).get("checked_at") or "")
    out = {}
    for p in repo.get("prs") or []:
        if isinstance(p, dict) and p.get("state") in ("open", "draft") and p.get("head") and p.get("number") is not None:
            name = "PR #%s head" % p["number"]
            out[name] = {"name": name, "kind": "pull_request", "head": str(p["head"]).lower(), "observed_at": checked}
    here = _place(location)
    if here and here == (identity or {}).get("tracked"):
        for b in repo.get("branches") or []:
            if not isinstance(b, dict) or not b.get("branch") or b.get("status") == "abandoned":
                continue
            known = (branches or {}).get(b["branch"]) or {}
            own = [c.get("sha") for c in b.get("commits") or [] if isinstance(c, dict) and c.get("sha")]
            tip = known.get("tip") or (own[0] if own else "")
            if tip:
                name = "branch %s tip" % b["branch"]
                out[name] = {"name": name, "kind": "branch", "head": str(tip).lower(),
                             "observed_at": str(known.get("checked_at") or checked)}
    return [out[name] for name in sorted(out)]


def applicability(run, refs):
    """One run against each reference. → {state, reason, references: [{name, kind, head, result}], text}.

    state  `same commit` / `different commit` when every comparable reference names one commit; `ambiguous` when
           the references name different commits (the run is shown against each, none is preferred);
           `not comparable`; `unknown` with the reason when no head was captured or there is no reference."""
    status = run.get("status") if run.get("status") in RUN_STATES else "unknown"
    head = str(((run.get("context") or {}) if isinstance(run.get("context"), dict) else {}).get("head") or "").lower()
    if same_head(head, head) is None:
        return {"state": "unknown", "reason": NOT_CAPTURED, "references": [],
                "text": "%s; applicability unknown: %s" % (status, NOT_CAPTURED)}
    seen = "%s; HEAD observed after the run was %s" % (status, head[:MIN_HEAD])
    if not refs:
        return {"state": "unknown", "reason": NOTHING_TO_COMPARE, "references": [],
                "text": "%s; applicability unknown: %s" % (seen, NOTHING_TO_COMPARE)}
    rows, heads = [], []
    for ref in refs:
        same = same_head(head, ref.get("head"))
        result = "not comparable" if same is None else "same commit" if same else "different commit"
        rows.append({"name": ref["name"], "kind": ref.get("kind"), "head": ref.get("head"), "result": result})
        if same is not None and not any(same_head(ref["head"], other) for other in heads):
            heads.append(ref["head"])
    results = {row["result"] for row in rows} - {"not comparable"}
    if len(heads) > 1:
        state, reason = "ambiguous", "the references name different commits"
    elif not results:
        state, reason = "not comparable", "no reference has a comparable commit id"
    else:
        state, reason = results.pop(), ""
    text = seen + "".join("; %s is %s" % (row["name"], str(row["head"] or "")[:MIN_HEAD] or "unknown") for row in rows)
    return {"state": state, "reason": reason, "references": rows, "text": text}


def test_nodes(tk, identities, ticket, branches=None):
    """Captured local test runs: a `test_case` per command, a `test_run` per record, and each run's applicability
    against the references of its own repository and location. Ticket-level evidence: no requirement is touched.

    branches  {repo_key: branches.json} when the caller has them (see `references`)
    → {nodes, edges}"""
    key = (tk or {}).get("id") or ""
    nodes, edges = {}, {}
    tests = (tk or {}).get("tests") or {}
    for tid in sorted(tests):
        t = tests[tid]
        if not isinstance(t, dict):
            continue
        ctx = evidence_rows.captured_context(t.get("context")) or {}
        location = _place(ctx.get("location"))
        ident = _identity(identities, ctx["location"])[1] if ctx.get("location") else None
        repo_id = ctx.get("repo_id") or ((ident["locations"].get(location) or ident["id"]) if ident else "")
        refs = references(tk, ident, location, (branches or {}).get(ident.get("repo_key"))) if ident else []
        status = t.get("status") if t.get("status") in RUN_STATES else "unknown"
        command = " ".join(str(t.get("command") or "").split())
        ref = "ticket:%s/test:%s" % (key, tid)
        case = _put(graphmodel.node("test_case", (ticket, repo_id, command), key, command or "test command",
                                    {"test_type": "command", "command": command}, repo_id=repo_id, source_ref=ref),
                    nodes)
        counts = {k: v for k, v in (t.get("counts") or {}).items()
                  if isinstance(v, int) and not isinstance(v, bool)} if isinstance(t.get("counts"), dict) else {}
        run = _put(graphmodel.node(
            "test_run", (ticket, tid), key, "%s: %s" % (status, command or "test command"),
            {"status": status, "counts": counts, "source": "local", "head": ctx.get("head") or "",
             "head_basis": t.get("head_basis") or "", "context": sorted(ctx), "evidence": "test:" + tid,
             "applicability": applicability(dict(t, context=ctx), refs)},
            repo_id=repo_id, location=location, observed_at=str(t.get("at") or ""), source_ref=ref), nodes)
        _put(graphmodel.edge("EXECUTED_IN", case, run, key, "observed", evidence_ref=ref), edges)
        _put(graphmodel.edge("HAS_EVIDENCE", ticket, run, key, "observed", evidence_ref=ref,
                             attrs={"how": "session turn"}), edges)
    return _sorted(nodes, edges)


# --- the developer's mappings (Companion evidence pins) and implementation status (design sections 6, 10, 11) ---
# A requirement-level link exists only where a developer declared it. The one heuristic here, an exact path match,
# produces inferred edges that are listed and never counted.

PINNED_COPY = "pinned copy; source no longer retained"
LEGACY_UNBOUND = "legacy-unbound"
EXACT_PATH = "exact-path"
PATH_CONFIDENCE = 0.5
_IMPLEMENTS = ("commit", "file")
_TESTS = ("test", "ci")
_MANUAL = ("acceptance", "code_inspection")


def _plan_lives(workflow):
    """{plan id: lifecycle}, as `plan_nodes` decides it: versions before the judged plan are superseded."""
    plans = workflow.get("plans") or {}
    order = [pid for pid in workflow.get("plan_order") or sorted(plans) if pid in plans]
    judged = judged_plan(workflow)[0]
    pivot = order.index(judged) if judged in order else len(order) - 1
    return {pid: "superseded" if n < pivot else "current" for n, pid in enumerate(order)}


def _step_targets(workflow, pin, lives):
    """Where a pin's step ids point. → [(plan id, step id, ambiguous)].

    A pin that records its plan points into that plan only. A pin saved before plans were recorded is bound to a
    step only where that can be proven: one saved plan has the id, or every plan that has it words the step the
    same. Otherwise each candidate is returned as ambiguous and nothing is chosen."""
    plans, out = workflow.get("plans") or {}, []
    for sid in pin.get("steps") or []:
        if pin.get("plan"):
            if any(s["id"] == sid for s in (plans.get(pin["plan"]) or {}).get("steps") or []):
                out.append((pin["plan"], sid, False))
            continue
        found = [(pid, s) for pid in lives for s in plans[pid].get("steps") or [] if s["id"] == sid]
        if len(found) == 1 or (found and len({planning.norm(s.get("text")) for _, s in found}) == 1):
            judged = judged_plan(workflow)[0]
            pid = judged if any(p == judged for p, _ in found) else found[-1][0]
            out.append((pid, sid, False))
        else:
            out += [(pid, sid, True) for pid, _ in found]
    return out


def mapping_edges(workflow, requirement_index, graph):
    """Project one workflow's evidence pins into requirement-level edges.

    graph  {nodes, edges} built so far for the ticket (commits, code changes, test runs, CI runs): a pin is linked
           to the node of the source it names; a source the ticket record no longer holds gets a node made from
           the pinned copy, `lifecycle: source_missing`
    → {nodes, edges, unprojected}: new nodes only (observations, pinned copies), the declared edges, the inferred
      exact-path edges, and the ids of pins whose kind has no requirement-level edge (pull requests, decisions)."""
    wid, binding = workflow["id"], workflow.get("binding") or {}
    key, ticket, repo_id = binding.get("ticket") or "", requirement_index.get("ticket"), binding.get("repo_id") or ""
    have = {row["id"]: row for row in graph.get("nodes") or []}
    by_evidence = {row["attrs"].get("evidence"): row["id"] for row in have.values() if row["type"] == "test_run"}
    case_of = {row["dst"]: row["src"] for row in graph.get("edges") or [] if row["type"] == "EXECUTED_IN"}
    commits, changes = {}, {}
    for row in sorted(have.values(), key=lambda r: (r.get("repo_id") != repo_id, r["id"])):
        if row["type"] == "commit":
            commits.setdefault(row["attrs"].get("sha"), row["id"])
        elif row["type"] == "code_change" and row.get("repo_id") in (repo_id, ""):
            changes.setdefault(str(row["attrs"].get("path") or "").replace("\\", "/"), []).append(row["id"])
    mapped = (requirement_index.get("baselines") or {}).get(wid) or {}
    lives = _plan_lives(workflow)
    place = {"workflow": wid, "repo_id": repo_id, "location": _place(binding.get("location"))}
    nodes, edges, unprojected = {}, {}, []

    def missing(kind, identity, label, attrs, pin):
        """A node made from the pinned copy of a source the ticket record no longer holds."""
        return _put(graphmodel.node(kind, identity, key, label or pin.get("label") or pin["id"],
                                    dict(attrs, pinned=True, note=PINNED_COPY), lifecycle="source_missing",
                                    repo_id=repo_id, observed_at=str(pin.get("observed_at") or ""),
                                    source_ref="workflow:%s/evidence:%s" % (wid, pin["id"])), nodes)

    def declare(kind, src, dst, scope, pin, attrs, life="current"):
        row = graphmodel.edge(kind, src, dst, key, "declared", scope=scope, attrs=attrs, lifecycle=life, workflow=wid,
                              evidence_ref="workflow:%s/evidence:%s" % (wid, pin["id"]),
                              observed_at=str(pin.get("mapped_at") or ""))
        row = edges.setdefault(row["id"], row)   # the same edge declared again keeps every actor and time
        graphmodel.add_declaration(row, pin.get("mapped_by") or "", pin.get("mapped_at") or "", pin["id"])

    def targets(pin):
        """→ (the nodes an implementation pin names, the test case, the run or observation)."""
        kind, _, rest = pin["id"].partition(":")
        if pin["kind"] == "commit":
            return [commits.get(rest) or missing("commit", (repo_id, rest), "", {"sha": rest, "short": rest[:9],
                                                                                 "mine": "unknown"}, pin)], None, None
        if pin["kind"] == "file":
            path = rest.replace("\\", "/")
            return changes.get(path) or [missing("code_change", (repo_id, UNKNOWN_LOCATION, path, "pinned"), path,
                                                 {"path": path, "state": "unknown", "verified": False}, pin)], None, None
        if pin["kind"] in _TESTS:
            run = by_evidence.get(pin["id"])
            if run is None:
                head = ((pin.get("context") or {}) if isinstance(pin.get("context"), dict) else {}).get("head") or ""
                parts = pin["id"].split(":")
                identity = (ticket, rest) if pin["kind"] == "test" else (parts[1], repo_id, parts[-2], pin.get("attempt"))
                run = missing("test_run", identity, "", {
                    "status": pin.get("outcome") if pin.get("outcome") in RUN_STATES else "unknown",
                    "source": "local" if pin["kind"] == "test" else "ci", "head": head, "evidence": pin["id"]}, pin)
            return [], case_of.get(run), run
        note = _put(graphmodel.node(
            "observation", (wid, pin["id"]), key, pin.get("label") or pin["id"],
            {"kind": pin["kind"], "outcome": pin.get("outcome"), "revision": pin.get("revision") or "",
             "actor": pin.get("actor") or pin.get("mapped_by") or "", "limits": pin.get("limits") or "",
             "basis": "self-reported", "evidence": pin["id"]},
            observed_at=str(pin.get("observed_at") or ""), source_ref="workflow:%s/evidence:%s" % (wid, pin["id"]),
            **place), nodes)
        return [], None, note

    for pin in workflow.get("evidence") or []:
        if not isinstance(pin, dict) or not isinstance(pin.get("id"), str) or not pin.get("kind"):
            continue
        if pin["kind"] not in _IMPLEMENTS + _TESTS + _MANUAL:
            unprojected.append(pin["id"])
            continue
        made, case, proof = targets(pin)
        criteria = mapped.get(pin.get("baseline")) or {}
        revisions = [(cid, revision_id(criteria[cid]["requirement"], criteria[cid]["revision"]))
                     for cid in pin.get("criteria") or [] if cid in criteria]
        common_attrs = {"criterion": None, "baseline": pin.get("baseline") or "", "pin": pin["id"]}
        for cid, revision in revisions:
            attrs = dict(common_attrs, criterion=cid)
            for node in made:
                declare("IMPLEMENTED_BY", revision, node, wid, pin, attrs)
            if proof:
                if case:
                    declare("COVERED_BY_TEST", revision, case, wid, pin, attrs)
                declare("SUPPORTED_BY", revision, proof, wid, pin,
                        dict(attrs, coverage=pin.get("coverage") or "partial", outcome=pin.get("outcome"),
                             kind=pin["kind"], steps=sorted(pin.get("steps") or [])))
        for pid, sid, ambiguous in _step_targets(workflow, pin, lives) if made else []:
            pnode = graphmodel.node_id("plan", wid, pid)
            attrs = {"step": sid, "pin": pin["id"], "plan_binding": "recorded" if pin.get("plan") else "proven"}
            if ambiguous:   # listed, never counted toward "Linked"
                attrs.update(ambiguous=True, plan_binding=LEGACY_UNBOUND)
            for node in made:
                declare("IMPLEMENTED_BY", graphmodel.node_id("plan_step", pnode, sid), node, pnode, pin, attrs,
                        lives.get(pid, "current"))

    for pid, life in lives.items():   # the one heuristic: a file the plan names is a file that changed
        named = {str(f.get("path") or "").replace("\\", "/") for f in
                 ((workflow["plans"][pid].get("details") or {}).get("files") or []) if isinstance(f, dict)}
        pnode = graphmodel.node_id("plan", wid, pid)
        for path in sorted(named & set(changes)):
            for node in changes[path]:
                row = graphmodel.proposal({"schema": 1, "origin": EXACT_PATH, "type": "IMPLEMENTED_BY", "src": pnode,
                                           "dst": node, "confidence": PATH_CONFIDENCE,
                                           "rationale": "the plan names a file path that equals a changed path"},
                                          ticket=key, scope=pnode)
                row.update(workflow=wid, lifecycle=life)
                row["version"] = graphmodel.version(row)
                edges.setdefault(row["id"], row)
    return dict(_sorted(nodes, edges), unprojected=sorted(unprojected))


def implementation_rows(workflow, requirement_index, edges, ticket_has_changes):
    """Implementation evidence per requirement: for one workflow (the requirements its current baseline confirms,
    judged against its judged plan's implementation steps), or with `workflow` None the ticket-level rows, where
    nothing is linked to a requirement and the only question is whether the ticket has changes at all.
    → {gr: {state, basis, reason, edges}}"""
    reqs = requirement_index.get("requirements") or {}
    if workflow is None:
        return {rid: graphmodel.implementation([], [], ticket_has_changes) for rid in reqs}
    wid = workflow["id"]
    baselines = (requirement_index.get("baselines") or {}).get(wid) or {}
    judged = judged_plan(workflow)[0]
    plan = (workflow.get("plans") or {}).get(judged) or {}
    planned = {entry["requirement"]: cid for cid, entry in (baselines.get(plan.get("baseline")) or {}).items()}
    pnode = graphmodel.node_id("plan", wid, judged) if judged else None
    mine = [row for row in edges if row.get("workflow") == wid and row["type"] == "IMPLEMENTED_BY"]
    out = {}
    for entry in (baselines.get(workflow.get("baseline")) or {}).values():
        rid = entry["requirement"]
        cid = planned.get(rid)
        steps = [graphmodel.node_id("plan_step", pnode, s["id"]) for s in plan.get("steps") or []
                 if cid is not None and s.get("kind", "implementation") == "implementation"
                 and cid in (s.get("criteria") or [])]
        sources = set(steps) | {revision_id(rid, entry["revision"])}
        out[rid] = graphmodel.implementation([row for row in mine if row["src"] in sources], steps,
                                             ticket_has_changes)
    return out
