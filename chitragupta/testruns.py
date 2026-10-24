"""Test runs, captured deterministically (no model): a Bash/PowerShell call whose command runs a test runner.

The hook keeps only numbers from the tool's output (status, exit code, pass/fail counts: hook._outcome "test"); the
agent records one `test` per call on the turn's ticket (tk["tests"][id]). The ticket brief cites them as test:<id>.
"""
import re

from . import common

_RUNNERS = re.compile(
    r"(?:^|[;&|(]\s*|\s)(?:"
    r"py\.?test\b|pytest\b"
    r"|(?:python[0-9.]*|py)(?:\s+-[0-9A-Za-z.]+)*\s+-m\s+(?:pytest|unittest)\b"
    r"|(?:\./)?mvnw?(?:\.cmd)?\s+(?:[^;&|\n]*\s)?(?:test|verify)\b"
    r"|(?:\./|\.\\)?gradlew?(?:\.bat)?\s+(?:[^;&|\n]*\s)?test\b"
    r"|(?:npm|pnpm|yarn)\s+(?:run\s+)?test\b"
    r"|npx\s+(?:jest|vitest|mocha)\b|jest\b|vitest\b"
    r"|go\s+test\b|dotnet\s+test\b|cargo\s+test\b|bru\s+run\b)", re.I)
_ECHO = re.compile(r"^\s*(?:echo|cat|ls|dir|type|grep|rg|find)\b", re.I)


def is_test_command(cmd):
    """True when a shell command runs a test runner (pytest, python -m unittest, mvn test, gradle test, npm test,
    go test, dotnet test, bru run, …)."""
    cmd = str(cmd or "")[:2000]
    if not cmd.strip():
        return False
    for part in re.split(r"&&|\|\||;|\n", cmd):
        if part.strip() and not _ECHO.match(part) and _RUNNERS.search(" " + part.strip()):
            return True
    return False


_WORDS = {"passed": "passed", "pass": "passed", "passing": "passed", "ok": "passed", "succeeded": "passed",
          "failed": "failed", "failures": "failed", "failure": "failed", "fail": "failed", "failing": "failed",
          "errors": "errors", "error": "errors", "skipped": "skipped", "skip": "skipped", "pending": "skipped",
          "total": "total", "tests run": "total"}


def parse_counts(text):
    """Pass/fail counts from a test runner's summary lines, or {}: pytest ("2 failed, 10 passed"), unittest ("Ran 12
    tests" + "OK" / "FAILED (failures=1)"), surefire ("Tests run: 9, Failures: 0, …"), jest / bru ("Tests: 1 failed,
    11 passed, 12 total"), dotnet ("Failed: 1, Passed: 11")."""
    text = str(text or "")[-20000:]
    out = {}
    m = list(re.finditer(r"\bRan (\d+) tests? in [\d.]+s", text))
    if m:
        out["total"] = int(m[-1].group(1))
        tail = text[m[-1].end():]
        f = re.search(r"\bFAILED \(([^)]*)\)", tail)
        if f:
            for k, v in re.findall(r"(failures|errors|skipped|expected failures)=(\d+)", f.group(1)):
                out[_WORDS.get(k, k)] = int(v)
        elif re.search(r"^OK\b", tail, re.M):
            out["passed"] = out["total"] - sum(int(v) for v in re.findall(r"skipped=(\d+)", tail))
        return out
    m = list(re.finditer(r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)(?:,\s*Skipped:\s*(\d+))?", text))
    if m:
        g = m[-1]
        out = {"total": int(g.group(1)), "failed": int(g.group(2)), "errors": int(g.group(3))}
        if g.group(4) is not None:
            out["skipped"] = int(g.group(4))
        return out
    m = list(re.finditer(r"(?:Failed|Passed)!\s*-\s*Failed:\s*(\d+),\s*Passed:\s*(\d+)(?:,\s*Skipped:\s*(\d+))?", text))
    if m:
        g = m[-1]
        out = {"failed": int(g.group(1)), "passed": int(g.group(2))}
        if g.group(3) is not None:
            out["skipped"] = int(g.group(3))
        return out
    # "Tests: 1 failed, 11 passed, 12 total" (jest, bru) or "=== 2 failed, 10 passed in 3s ===" (pytest)
    for line in reversed(text.splitlines()):
        pairs = re.findall(r"(\d+)\s+(passed|failed|errors?|skipped|total|pending|passing|failing)\b", line, re.I)
        if pairs and (re.search(r"\bin [\d.]+\s*m?s\b|=====|^\s*Tests?:|total", line, re.I) or len(pairs) > 1):
            for n, w in pairs:
                k = _WORDS.get(w.lower(), w.lower())
                out[k] = out.get(k, 0) + int(n)
            return out
    return {}


def _exit_code(resp):
    for k in ("exit_code", "exitCode", "returncode", "return_code", "returnCode"):
        v = resp.get(k)
        if isinstance(v, int) and not isinstance(v, bool):
            return v
    return None


def outcome(resp):
    """{"status": passed|failed|interrupted|unknown, "exit"?: n, "counts"?: {...}} from a tool_response. Only numbers
    and the status are kept: never any output text."""
    r = resp if isinstance(resp, dict) else {"stdout": str(resp or "")}
    counts = parse_counts("%s\n%s" % (r.get("stdout") or "", r.get("stderr") or ""))
    code = _exit_code(r)
    bad = int(counts.get("failed") or 0) + int(counts.get("errors") or 0)
    if r.get("interrupted"):
        status = "interrupted"
    elif bad or (code is not None and code != 0):
        status = "failed"
    elif counts.get("passed") or code == 0:
        status = "passed"
    else:
        status = "unknown"
    out = {"status": status}
    if code is not None:
        out["exit"] = code
    if counts:
        out["counts"] = counts
    return out


KEEP = 50  # newest test records kept per ticket


def record(store, t, ev, day, sid):
    """One test record on ticket `t` for a post-tool event that ran a test runner. Returns the record or None."""
    p = ev.get("payload") or {}
    cmd = str((p.get("input") or {}).get("command") or "")
    if not t or not is_test_command(cmd):
        return None
    from .redact import redact
    oc = (p.get("outcome") or {}).get("test") or {}
    tid = common.stable_id(t, "test", "%s|%s|%s" % (sid, ev.get("ts"), cmd))
    tk = store.ticket(t)
    tests = tk.setdefault("tests", {})
    if tid in tests:
        return tests[tid]
    rec = {"id": tid, "at": ev.get("ts") or "", "day": day, "session": (sid or "")[:8],
           "command": common.truncate(redact(" ".join(cmd.split())), 200), "status": oc.get("status") or "unknown"}
    if oc.get("exit") is not None:
        rec["exit"] = oc["exit"]
    if oc.get("counts"):
        rec["counts"] = oc["counts"]
    from .store import raw_ref
    ref = raw_ref(day, sid, ev.get("_ln"))
    if ref:
        rec["ref"] = ref
    tests[tid] = rec
    for k in sorted(tests, key=lambda k: tests[k].get("at") or "")[:-KEEP]:
        del tests[k]
    store.touch_ticket(t)
    return rec
