"""End-to-end self-test in a throwaway data home with a scratch repo and a local bare remote (opt-in: CHITRAGUPTA_E2E=1).

    py -3 scripts/e2e/e2e_selftest.py --simulated   feed the hook launcher directly, then a real `agent tick --force`
    py -3 scripts/e2e/e2e_selftest.py --live        a real `claude -p` session whose only hooks are Chitragupta's
                                                    (passed with --settings; user settings are not loaded)
    add --keep to leave the temp folder for inspection

Asserts: events from all five hooks captured; tickets/ABC-1001/README.md has >= 1 finding; daily file and catalogs
rendered; the commit reached the bare repo; a second tick adds no duplicates. Real model calls (extract/consolidate).
"""
import glob
import json
import os
import subprocess
import sys

from e2e_common import PY, RUN_PY, Checks, TempEnv, gate, paths_cfg

TICKET = "ABC-1001"
NOWIN = 0x08000000 if os.name == "nt" else 0


def run(args, cwd=None, env=None, inp=None, timeout=600):
    p = subprocess.run(args, cwd=cwd, env=env, input=inp, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, creationflags=NOWIN)
    return p.returncode, p.stdout, p.stderr


RETRY_JOB = """package demo;

public class RetryJob {
    // Re-sends pending SMS notifications every 5 minutes.
    public void run(Notification n) {
        if (n.getRetryCount() < 3) {
            gateway.send(n);
            n.setRetryCount(n.getRetryCount() + 1);   // line 8: incremented in memory only
            // BUG: repository.save(n) is never called, so retry_count stays 0 in notification_queue
        }
    }
}
"""


def main(argv):
    gate("e2e_selftest")
    live = "--live" in argv
    te = TempEnv("cg-e2e-selftest-")
    from chitragupta import common, install
    home, repo, bare, tdir = te.home, *(os.path.join(te.tmp, x) for x in ("repo", "remote.git", "transcripts"))
    for d in (repo, tdir):
        os.makedirs(d)
    paths = paths_cfg()
    git = paths["git"]
    email = "selftest@acme.test"
    cfg = {"github_remote": bare.replace("\\", "/"), "ticket_prefixes": ["ABC"],
           "capture_scope": {"include_paths": [], "exclude_paths": []}, "watched_repos": [repo.replace("\\", "/")],
           "git_author_emails": [email], "day_starts_at": "04:00",
           "models": {"extract": "haiku", "consolidate": "sonnet"}, "paths": paths, "push_interval_minutes": 30,
           "mcp": {"jira": {"enabled": False}, "database": {"enabled": False}}}
    common.write_json(os.path.join(home, ".worklog", "config.json"), cfg)
    env = te.child_env(CHITRAGUPTA_TRANSCRIPTS_DIR=tdir, GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_EMAIL=email)
    run([git, "init", "--bare", "-q", "-b", "main", bare], env=env)
    run([git, "init", "-q", "-b", "feature/%s-selftest" % TICKET, repo], env=env)
    os.makedirs(os.path.join(repo, "src"))
    with open(os.path.join(repo, "src", "RetryJob.java"), "w", encoding="utf-8") as f:
        f.write(RETRY_JOB)
    run([git, "-C", repo, "add", "-A"], env=env)
    run([git, "-C", repo, "commit", "-q", "-m", "%s: add retry job" % TICKET], env=env)

    ck = Checks()
    try:
        if live:
            settings = os.path.join(te.tmp, "settings.json")
            common.write_json(settings, {"hooks": {ev: [grp] for ev, grp in install.hook_entries().items()}})
            prompt = ("Read src/RetryJob.java, then run `git log --oneline -1`. In two sentences explain why notifications "
                      "are re-sent on every retry. End your reply with one line starting exactly with 'Worklog note:' "
                      "stating the root cause with file:line and the table/column involved.")
            rc, so, se = run([paths["claude"], "-p", prompt, "--model", "haiku", "--no-session-persistence",
                              "--setting-sources", "project", "--settings", settings,
                              "--allowedTools", "Read", "Bash(git log *)", "--permission-mode", "dontAsk"],
                             cwd=repo, env=env, timeout=600)
            print("claude rc=%s\n--- reply ---\n%s\n-------------" % (rc, so.strip()[:1500]))
            if rc != 0:
                print(se[:1000])
        else:
            sid = "selftest-0001"
            msg = ("RetryJob.run increments retry_count only in memory (src/RetryJob.java:8) and never calls "
                   "repository.save, so notification_queue.retry_count stays 0 and every run re-sends.\n"
                   "Worklog note: Duplicate SMS root cause - RetryJob.run never persists retry_count "
                   "(src/RetryJob.java:8; notification_queue.retry_count stays 0).")
            for ev, payload in (("session-start", {"source": "startup"}),
                                ("user-prompt", {"user_prompt": "Why does %s resend SMS on every retry? Read src/RetryJob.java" % TICKET}),
                                ("post-tool", {"tool_name": "Read", "tool_input": {"file_path": os.path.join(repo, "src", "RetryJob.java")}}),
                                ("post-tool", {"tool_name": "Bash", "tool_input": {"command": "git log --oneline -1"}}),
                                ("stop", {"last_assistant_message": msg}),
                                ("session-end", {"reason": "other"})):
                payload.update({"session_id": sid, "cwd": repo, "transcript_path": ""})
                run([PY, "-B", RUN_PY, "hook", ev], env=env, inp=json.dumps(payload))

        raw = []
        for p in glob.glob(os.path.join(home, ".worklog", "raw", "*", "*.jsonl")):
            with open(p, encoding="utf-8") as f:
                raw += [json.loads(l) for l in f if l.strip()]
        events = {e["event"] for e in raw}
        ck(events >= {"session-start", "user-prompt", "post-tool", "stop", "session-end"},
           "events from all five hooks captured (%s)" % sorted(events))
        ck(any(e.get("ticket") == TICKET for e in raw), "ticket %s detected from branch" % TICKET)

        tick = [PY, "-B", RUN_PY, "agent", "tick", "--force"]
        rc, so, se = run(tick, env=env, timeout=1200)
        ck(rc == 0, "agent tick --force exit 0" + ("" if rc == 0 else ": " + se[-500:]))
        tk = common.read_json(os.path.join(home, "data", "tickets", TICKET + ".json"))
        finds = [i for i in (tk.get("items") or {}).values() if i.get("kind") not in ("todo", "question", "blocker")]
        readme = os.path.join(home, "tickets", TICKET, "README.md")
        ck(len(finds) >= 1 and os.path.exists(readme), "tickets/%s/README.md has %d finding(s)" % (TICKET, len(finds)))
        daily = glob.glob(os.path.join(home, "daily", "*", "*", "*.md"))
        ck(bool(daily), "daily file rendered")
        ck(all(os.path.exists(os.path.join(home, "knowledge", n)) for n in ("db-tables.md", "apis.md", "integrations.md"))
           and os.path.exists(os.path.join(home, "tickets", "INDEX.md")), "catalogs + index rendered")
        rc, log1, _ = run([git, "--git-dir", bare, "log", "--oneline", "main"], env=env)
        ck(rc == 0 and log1.strip(), "commit reached the bare remote (%d commit(s))" % len(log1.strip().splitlines()))
        daily_txt = open(daily[0], encoding="utf-8").read() if daily else ""
        ck(TICKET in daily_txt and "add retry job" in daily_txt, "daily file lists the ticket and the commit")

        snap = json.dumps(tk, sort_keys=True)
        run(tick, env=env, timeout=1200)
        tk2 = common.read_json(os.path.join(home, "data", "tickets", TICKET + ".json"))
        _, log2, _ = run([git, "--git-dir", bare, "log", "--oneline", "main"], env=env)
        ck(json.dumps(tk2, sort_keys=True) == snap and log2 == log1, "second tick adds no duplicates / no new commit")
        sids = {os.path.basename(p)[:-6] for p in glob.glob(os.path.join(home, ".worklog", "raw", "*", "*.jsonl"))}
        ck(len(sids) == 1, "extraction runs produced no raw events (raw sessions: %d)" % len(sids))
        health = common.read_json(os.path.join(home, ".worklog", "health.json"))
        print("health:", json.dumps({k: health.get(k) for k in ("last_tick_ok", "last_push", "privacy", "cost_by_day", "last_error")}))
        if os.path.exists(readme):
            print("\n--- excerpt of tickets/%s/README.md ---" % TICKET)
            print("\n".join(open(readme, encoding="utf-8").read().splitlines()[:30]))
    finally:
        te.close(keep="--keep" in argv)
    return ck.summary()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
