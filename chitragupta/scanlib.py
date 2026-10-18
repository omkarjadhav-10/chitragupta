"""Deny-list and secret scan: one implementation for the repo's pre-commit scan (scripts/scan.py) and, at runtime,
the gate every share bundle must pass (share.py).

Deny-list file: one rule per line. Plain lines match as case-insensitive substrings, lines starting with "re:" are
Python regexes, lines starting with "#" are comments. For sharing, a rule may start with "internal:" (e.g.
"internal: orders-db" or "internal:re:\\bshop-\\w+\\b"): an internal name your team may see, replaced by <internal>
at the public level. Every other rule is a term that must never leave the machine. The repo scan treats both alike.

Reads only; standard library only.
"""
import re

SECRET_RULES = [
    ("aws-access-key", r"\bAKIA[0-9A-Z]{16}\b"),
    ("github-token", r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    ("github-pat", r"\bgithub_pat_[A-Za-z0-9_]{50,}\b"),
    ("slack-token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    ("atlassian-token", r"\bATATT[A-Za-z0-9_\-=]{20,}"),
    ("anthropic-key", r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
    ("openai-key", r"\bsk-[A-Za-z0-9]{32,}\b"),
    ("private-key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ("basic-auth", r"(?i)\bbasic\s+[A-Za-z0-9+/]{20,}={0,2}"),
    ("bearer", r"(?i)\bbearer\s+[A-Za-z0-9._\-]{30,}"),
    ("assigned-secret",
     r"""(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token|client[_-]?secret)\b["']?\s*[:=]\s*["'][^"'\s{}$<>%]{8,}["']"""),
]

INTERNAL = "internal:"


class Rule(tuple):
    """(name, compiled regex) — unpacks like the repo scan's pairs — plus `internal`."""

    def __new__(cls, name, rx, internal=False):
        r = tuple.__new__(cls, (name, rx))
        r.internal = internal
        return r


def _rule(line, internal=False):
    if line.startswith("re:"):
        try:
            return Rule(line, re.compile(line[3:]), internal)
        except re.error:
            return None
    return Rule(line, re.compile(re.escape(line), re.IGNORECASE), internal)


def parse_denylist(text):
    """[Rule] from a deny-list's text. A regex that doesn't compile is skipped."""
    rules = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        internal = line.lower().startswith(INTERNAL)
        if internal:
            line = line[len(INTERNAL):].strip()
            if not line:
                continue
        r = _rule(line, internal)
        if r is not None:
            rules.append(r)
    return rules


def load_denylist(path):
    """[Rule] from a deny-list file; [] when the path is empty or not a file."""
    if not path:
        return []
    try:
        with open(path, encoding="utf-8-sig") as f:
            return parse_denylist(f.read())
    except (OSError, UnicodeDecodeError):
        return []


def secret_rules():
    return [Rule(name, re.compile(rx)) for name, rx in SECRET_RULES]


def find(text, deny, secrets):
    """[(line number, "deny-list"|"secret", rule name)] for every rule that matches a line of `text`."""
    hits = []
    for no, line in enumerate((text or "").splitlines(), 1):
        for name, rx in deny:
            if rx.search(line):
                hits.append((no, "deny-list", name))
        for name, rx in secrets:
            if rx.search(line):
                hits.append((no, "secret", name))
    return hits


def scan_text(label, text, deny, secrets):
    """The repo scan's report lines: "<label>:<line>: deny-list: <rule>" / "...: secret: <rule>"."""
    return ["%s:%d: %s: %s" % (label, no, kind, name) for no, kind, name in find(text, deny, secrets)]


def mask_internal(text, rules, stats=None, token="<internal>"):
    """Replace every match of the `internal:` rules with <internal>; counts land in stats["internal-name"]."""
    if not text or not isinstance(text, str):
        return text
    for r in rules:
        if not r.internal:
            continue
        text, n = r[1].subn(token, text)
        if n and stats is not None:
            stats["internal-name"] = stats.get("internal-name", 0) + n
    return text
