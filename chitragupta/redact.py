"""Secret and patient-data redaction.

`redact(text)` is idempotent: redact(redact(x)) == redact(x). A text is "clean" when
redact(text) == text, which is exactly what the pre-commit check tests.

Masks are shape-preserving where shape is useful (digits -> X, letters -> x,
punctuation kept) so a finding can still say "phones arrive as (XXX) XXX-XXXX".
Secrets become fixed tokens (****, [JWT], [PRIVATE KEY]).

Modes:
  local   (default) secrets + patient data: everything captured or written locally
  secrets secrets only: QA-environment requests, payloads, responses and query samples (QA data may stay literal)
  public  local + QA values (known QA ids/values, QA hosts, long digit runs, UUIDs) + SQL text and JSON payload
          blocks removed: daily files, the Slack message, anything that leaves the machine

CLI (python -m chitragupta redact ..., or "<python>" "<launcher>" redact ...):
  redact --check FILE...      exit 1 if any file needs masking (pre-commit)
  redact --public FILE...     apply the public pass in place
  redact --fix FILE...        mask files in place
  redact --check-staged       pre-commit hook entry point (staged files of the cwd repo)
"""
import json
import os
import re
import sys

SECRET = "****"
VAR_REF = re.compile(r"\{\{\s*[A-Za-z_$][\w.$-]{0,120}\s*\}\}")

PERSON_KEYS = (
    r"first_?name|last_?name|middle_?name|full_?name|patient_?name|given_?name|family_?name|given|family|"
    r"birth_?date|date_?of_?birth|dob|mrn|medical_?record_?(?:number|no)|ssn|social_?security(?:_?number)?|"
    r"phone(?:_?number)?|mobile(?:_?number)?|home_?phone|cell_?phone|email(?:_?address)?|"
    r"address(?:_?line)?[12]?|street(?:_?address)?|city|postal_?code|zip(?:_?code)?|"
    r"member_?id|subscriber_?id|policy_?number|accession(?:_?(?:number|no|id))?|patient_?id"
)

# --------------------------------------------------------------------------- helpers


def shape(s):
    out = []
    for ch in s:
        if ch.isdigit():
            out.append("X")
        elif ch in "Xx":  # already masked: keep, so masking is idempotent
            out.append(ch)
        elif ch.isalpha():
            out.append("x")
        else:
            out.append(ch)
    return "".join(out)


def _mask_quoted(lit):
    """Mask the inside of a quoted literal, keeping the quotes (and N prefix)."""
    m = re.match(r"^(N?)(['\"])(.*)(\2)$", lit, re.S)
    if m:
        return m.group(1) + m.group(2) + shape(m.group(3)) + m.group(4)
    return shape(lit)


# --------------------------------------------------------------------------- rules
# Each rule: (name, compiled regex, replacement function or string)

def _r(p, flags=0):
    return re.compile(p, flags)


_RULES = []


def rule(name, pattern, repl, flags=0):
    _RULES.append((name, _r(pattern, flags), repl))


# Private key blocks
rule("private-key",
     r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
     "[PRIVATE KEY]", re.S)

# JWTs
rule("jwt", r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}", "[JWT]")

# Provider tokens
rule("aws-access-key", r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[0-9A-Z]{16}\b", "[AWS-KEY]")
rule("github-token", r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})", "[GITHUB-TOKEN]")
rule("slack-token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}", "[SLACK-TOKEN]")
rule("slack-webhook", r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+", "https://hooks.slack.com/services/" + SECRET)
rule("openai-like-key", r"\b(?:sk|rk|pk)-(?:live|test|proj|ant)?-?[A-Za-z0-9_-]{20,}", "[API-KEY]")
rule("google-api-key", r"\bAIza[0-9A-Za-z_-]{35}\b", "[API-KEY]")
rule("azure-conn",
     r"(?i)\b(AccountKey|SharedAccessKey|SharedAccessSignature|sig|SharedSecret)=([^;\s&\"']+)",
     lambda m: m.group(1) + "=" + (SECRET if m.group(2) != SECRET else SECRET))

def _is_reference(val):
    """A variable reference, not a value: {{var}} (protected placeholder), ${VAR}, $VAR, $(command), already masked."""
    if not val:
        return False
    return val == SECRET or val.startswith(("$", "{{", "%")) or any(0xE000 <= ord(c) <= 0xF8FF for c in val)


# Authorization headers / bearer tokens
rule("auth-header",
     r"(?i)\b(authorization|proxy-authorization|x-api-key|api-key|x-auth-token|ocp-apim-subscription-key)"
     r"(\"?\s*[:=]\s*\"?)(?:(bearer|basic|token|digest|negotiate|api-key|apikey|api_key|key|sso-key)\s+)?([^\s\"',;]+)",
     lambda m: m.group(0) if _is_reference(m.group(4)) else
     m.group(1) + m.group(2) + ((m.group(3) + " ") if m.group(3) else "") + SECRET)
rule("bearer", r"(?i)\b(bearer|basic)\s+(?!\*\*\*\*)([A-Za-z0-9._~+/=-]{12,})", lambda m: m.group(1) + " " + SECRET)

# Credentials inside URLs: scheme://user:pass@host
rule("url-cred", r"\b([a-zA-Z][a-zA-Z0-9+.-]*://)([^:/@\s]+):([^@\s/]+)@",
     lambda m: m.group(0) if _is_reference(m.group(3)) else m.group(1) + m.group(2) + ":" + SECRET + "@")

# key=value / key: value secret assignments (connection strings, .env, JSON, YAML, properties)
_SECRET_KEY = (r"[A-Za-z0-9_.-]*(?:password|passwd|pwd|passphrase|secret|token|api[_-]?key|apikey|"
               r"access[_-]?key|private[_-]?key|client[_-]?secret|credentials?|auth[_-]?key|signing[_-]?key)"
               r"[A-Za-z0-9_.-]*")


def _assign_repl(m):
    val = m.group(4)
    if _is_reference(val) or val.startswith("<") \
            or val.lower() in ("null", "none", "true", "false", "undefined", "required", "string", "optional"):
        return m.group(0)
    if re.fullmatch(r"\d{1,5}", val):  # e.g. max_tokens=100
        return m.group(0)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*\(", val):
        return m.group(0)  # getToken() style code, not a value
    return m.group(1) + m.group(2) + m.group(3) + SECRET + m.group(3)


rule("secret-assign",
     r"(?i)(\"?\b" + _SECRET_KEY + r"\b\"?)(\s*(?:=|:|=>)\s*)([\"']?)([^\s\"',;&}{)\]]{3,})\3",
     _assign_repl)

# --- patient data -----------------------------------------------------------

# HL7 v2 segments (MSH kept: it carries routing metadata, not patient data)
_HL7_SEG = re.compile(r"(?m)(^|[\r\n]|(?<=\s))((?!MSH)[A-Z][A-Z0-9]{2})(\|[^\r\n]*)")


def _hl7_repl(m):
    rest = m.group(3)
    if rest.count("|") < 3:
        return m.group(0)
    fields = rest.split("|")
    masked = []
    for i, f in enumerate(fields):
        if i == 0:
            masked.append(f)
        elif i == 1 and re.fullmatch(r"\d{1,4}", f):  # set ID
            masked.append(f)
        else:
            masked.append(shape(f))
    return m.group(1) + m.group(2) + "|".join(masked)


_RULES.append(("hl7-segment", _HL7_SEG, _hl7_repl))

# SSN
rule("ssn", r"(?<![\dA-Za-z-])\d{3}-\d{2}-\d{4}(?![\d-])", "XXX-XX-XXXX")

# Email (git@host remotes and already-masked values left alone)
rule("email", r"(?<![\w.+-])(?!git@)([A-Za-z0-9._%+-]*[A-Za-z0-9])@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b",
     lambda m: m.group(0) if re.fullmatch(r"[x.X_%+-]+@[x.X-]+", m.group(0)) else shape(m.group(0)))

# Phone numbers (NANP-style and +CC forms)
rule("phone",
     r"(?<![\w-])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?![\w-])",
     lambda m: shape(m.group(0)))

# Person fields in SQL predicates:  mrn = '00123456', p.last_name LIKE N'Sm%', dob IN ('..','..')
rule("sql-person",
     r"(?i)(\b(?:[A-Za-z_][A-Za-z0-9_]*\.)?\[?\"?(?:" + PERSON_KEYS + r")\"?\]?\s*(?:=|<>|!=|\blike\b|\bin\b)\s*)"
     r"(\((?:\s*N?'[^']*'\s*,?)+\)|N?'[^']*'|\d{3,})",
     lambda m: m.group(1) + re.sub(r"N?'[^']*'|\d{3,}", lambda x: _mask_quoted(x.group(0)), m.group(2)))

# Person fields in INSERT ... VALUES is hard to align; JSON / Mongo / YAML style key: value
rule("json-person",
     r"(?i)([\"']?\b(?:" + PERSON_KEYS + r")\b[\"']?\s*:\s*)(\"(?:[^\"\\]|\\.)*\"|'[^']*'|\[[^\]]*\]|\d[\d-]{3,})",
     lambda m: m.group(1) + re.sub(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'|\d[\d-]{3,}",
                                   lambda x: _mask_quoted(x.group(0)), m.group(2)))

# FHIR identifier / telecom values:  "value": "00123456"
rule("fhir-value", r"(\"value\"\s*:\s*\")([^\"]*\d[^\"]*)(\")",
     lambda m: m.group(1) + (m.group(2) if not re.search(r"\d{4,}", m.group(2)) else shape(m.group(2))) + m.group(3))

# Prose: "MRN 00123456", "DOB: 01/02/1980", "accession # A12345"
rule("prose-person",
     r"(?i)\b(mrn|dob|date of birth|ssn|member id|accession(?: number| no\.?| #| id)?)(\s*[:=#]?\s*)"
     r"([A-Za-z]{0,3}\d[\d/.-]{3,}[A-Za-z0-9]*)",
     lambda m: m.group(1) + m.group(2) + shape(m.group(3)))


# --------------------------------------------------------------------------- engine

_cfg_cache = {}


def _config_patterns():
    if "loaded" in _cfg_cache:
        return _cfg_cache["extra"], _cfg_cache["allow"]
    extra, allow = [], []
    try:
        from . import common  # noqa
        cfg = common.load_config()
        for p in cfg.get("extra_sensitive_patterns") or []:
            try:
                extra.append(re.compile(p))
            except re.error:
                pass
        for e in cfg.get("git_author_emails") or []:  # my own commit identities are not patient data
            if e:
                allow.append(re.compile(re.escape(e), re.I))
        for p in cfg.get("allow_patterns") or []:
            try:
                allow.append(re.compile(p))
            except re.error:
                pass
    except Exception:
        pass
    _cfg_cache.update(loaded=True, extra=extra, allow=allow)
    return extra, allow


PATIENT_RULES = {"hl7-segment", "ssn", "email", "phone", "sql-person", "json-person", "fhir-value", "prose-person"}

# --------------------------------------------------------------------------- public pass (leaves the machine)

_FENCE = re.compile(r"(?ms)^([ \t]*)(```|~~~)[ \t]*([A-Za-z0-9_+-]*)[^\n]*\n(.*?)^\1\2[ \t]*$")
_SQL_WORDS = (r"select\s+(?:top\s+\d+\s+|distinct\s+)?[\w*.,\s()\[\]\"]{1,300}?\s+from\s+[\w.\[\]\"]+|"
              r"select\s+from\s+[\w.\[\]\"]+\s+(?:\w+\s+)?(?:where|join|inner|left|on)\b|"
              r"update\s+[\w.\[\]\"]+\s+set\s+\w|insert\s+into\s+[\w.\[\]\"]+|delete\s+from\s+[\w.\[\]\"]+|"
              r"alter\s+table\s+\w|create\s+(?:table|index|view|procedure)\s+\w")
_SQL_INLINE = re.compile(r"(?is)`[^`\n]*\b(?:" + _SQL_WORDS + r")[^`\n]*`")
_SQL_BARE = re.compile(r"(?i)\b(?:" + _SQL_WORDS + r")[^\n]*")
_JSON_INLINE = re.compile(r"`\{[^`\n]{20,}\}`")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_DIGITS = re.compile(r"(?<![\w.:/-])\d{5,}(?![\w])")
_QA_HOST = re.compile(r"\b(?:https?://)?[a-z0-9.-]*(?:-qa\b|\bqa-|\.qa\.|-dev\b|\bdev-|-uat\b|\buat-)[a-z0-9.-]*\.[a-z]{2,}(?::\d+)?",
                      re.I)
_public_cache = {}


def _qa_values():
    """Known QA literal values (test accounts, ids, names) that must not leave the machine."""
    if "vals" in _public_cache:
        return _public_cache["vals"]
    vals = set()
    try:
        from . import common  # noqa
        for line in common.read_text(common.hp("bruno", ".env")).splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'")
                if len(v) >= 4 and not re.search(r"(?i)pass|secret|token|key", k):
                    vals.add(v)
        for v in common.read_json(common.wl("cache", "qa_values.json"), []) or []:
            if isinstance(v, str) and len(v) >= 4:
                vals.add(v)
    except Exception:
        pass
    _public_cache["vals"] = sorted(vals, key=len, reverse=True)
    return _public_cache["vals"]


def _public(text, stats):
    def hit(name, n=1):
        if stats is not None and n:
            stats[name] = stats.get(name, 0) + n

    def fence(m):
        lang, body = (m.group(3) or "").lower(), m.group(4)
        if lang in ("sql", "tsql", "pgsql", "mysql", "mongo", "mongodb") or _SQL_BARE.search(body):
            hit("public-sql-block")
            return "%s_(query kept in the local notes)_" % m.group(1)
        if lang in ("json", "xml", "hl7", "http", "bru", "yaml", "yml") or body.lstrip().startswith(("{", "[", "<", "MSH|")):
            hit("public-payload-block")
            return "%s_(payload kept in the local notes)_" % m.group(1)
        return m.group(0)
    text = _FENCE.sub(fence, text)
    text, n = _SQL_INLINE.subn("`(query kept locally)`", text)
    hit("public-sql", n)
    text, n = _JSON_INLINE.subn("`(payload kept locally)`", text)
    hit("public-payload", n)
    text, n = _SQL_BARE.subn("(query kept locally)", text)
    hit("public-sql", n)
    for v in _qa_values():
        if v in text:
            hit("public-qa-value")
            text = text.replace(v, shape(v))
    for rx, name in ((_QA_HOST, "public-qa-host"), (_UUID, "public-uuid"), (_DIGITS, "public-digits")):
        def sub(m, _name=name):
            out = "<qa-host>" if _name == "public-qa-host" else shape(m.group(0))
            if out != m.group(0):
                hit(_name)
            return out
        text = rx.sub(sub, text)
    return text


def redact(text, stats=None, use_config=True, mode="local"):
    """Mask secrets and patient data (mode local), secrets only (secrets) or everything that must not leave the
    machine (public). `stats` (dict) collects rule-hit counts."""
    if not text or not isinstance(text, str):
        return text
    extra, allow = _config_patterns() if use_config else ([], [])
    protected = {}

    def _protect(m):
        key = chr(0xE000 + len(protected))  # one private-use char: no rule can match or reshape it
        protected[key] = m.group(0)
        return key
    # {{variable}} references (Bruno / replay templates) are names, never values: never masked
    text = VAR_REF.sub(_protect, text)
    for a in allow:
        text = a.sub(_protect, text)
    for name, rx, repl in _RULES:
        if mode == "secrets" and name in PATIENT_RULES:
            continue

        def _count(m, _name=name, _repl=repl):
            out = _repl(m) if callable(_repl) else _repl
            if stats is not None and out != m.group(0):
                stats[_name] = stats.get(_name, 0) + 1
            return out
        text = rx.sub(_count, text)
    if mode != "secrets":
        for rx in extra:
            text = rx.sub(lambda m: shape(m.group(0)), text)
    if mode == "public":
        text = _public(text, stats)
    for k, v in protected.items():
        text = text.replace(k, v)
    return text


def redact_obj(obj, stats=None, mode="local"):
    """Recursively redact every string inside a JSON-like structure."""
    if isinstance(obj, str):
        return redact(obj, stats, mode=mode)
    if isinstance(obj, list):
        return [redact_obj(x, stats, mode) for x in obj]
    if isinstance(obj, dict):
        return {k: redact_obj(v, stats, mode) for k, v in obj.items()}
    return obj


def is_clean(text, mode="local"):
    return redact(text, mode=mode) == text


def mode_for(rel):
    """Pushed paths get the public pass (only daily files and README.md are pushed)."""
    r = rel.replace("\\", "/")
    return "public" if r.startswith("daily/") or r == "README.md" else "local"


def fix_file(path, stats=None, mode="local"):
    """Mask a file in place. JSON files are redacted value-by-value. Returns True if changed."""
    try:
        with open(path, encoding="utf-8") as f:
            original = f.read()
    except (OSError, UnicodeDecodeError):
        return False
    if path.lower().endswith(".json"):
        try:
            data = json.loads(original)
            new = json.dumps(redact_obj(data, stats, mode), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
            if json.loads(new) == data:
                return False
        except ValueError:
            new = redact(original, stats, mode=mode)
    else:
        new = redact(original, stats, mode=mode)
    if new == original:
        return False
    from .safety import paths
    paths.write_text(path, new)
    return True


def file_needs_fix(path, mode="local"):
    try:
        with open(path, encoding="utf-8") as f:
            original = f.read()
    except (OSError, UnicodeDecodeError):
        return False
    if path.lower().endswith(".json"):
        try:
            data = json.loads(original)
            return redact_obj(data, mode=mode) != data
        except ValueError:
            pass
    return not is_clean(original, mode)


SCANNED_EXT = (".md", ".json", ".txt", ".sql", ".js", ".yml", ".yaml", ".bru", ".http", ".html", ".htm",
               ".mmd", ".puml", ".svg", ".csv")
SCRIPT_SCANNED_EXT = (".sh", ".py", ".ps1")  # only under tickets/ (replay.sh, investigation scripts)


def should_scan(rel):
    """Files the pre-commit scan covers. The engine's own source is code, never data."""
    r = rel.replace("\\", "/").lower()
    if r.startswith("daily/") or r == "readme.md":
        return True
    if r.startswith(".worklog/engine/"):
        return False
    if r.endswith(SCANNED_EXT):
        return True
    return r.endswith(SCRIPT_SCANNED_EXT) and r.startswith("tickets/")


def _staged_files():
    from . import common
    from .safety import commands
    rc, so, se = commands.run([common.git_exe(), "-C", common.home(), "diff", "--cached", "--name-only",
                               "--diff-filter=ACMR", "-z"], "git-home")
    if rc != 0:
        raise RuntimeError("git diff --cached failed: %s" % se.strip()[:200])
    return [p for p in so.split("\0") if p]


def main(argv):
    if not argv:
        print(__doc__)
        return 0
    mode, files = argv[0], argv[1:]
    if mode == "--check-staged":
        staged = _staged_files()
        try:
            from . import gitsync
            from . import common
            paths = gitsync.push_paths(common.load_config())
            outside = [f for f in staged if not gitsync.allowed_path(f, paths)]
        except Exception as e:  # noqa: refuse rather than push something unexpected
            sys.stderr.write("worklog pre-commit: allowlist check failed (%r)\n" % e)
            return 1
        if outside:
            sys.stderr.write("worklog pre-commit: only daily work is pushed; these paths stay local:\n  " +
                             "\n  ".join(outside) + "\nUnstage them (git restore --staged <path>).\n")
            return 1
        files = [f for f in staged if should_scan(f)]
        bad = [f for f in files if os.path.exists(f) and file_needs_fix(f, mode_for(f))]
        if bad:
            sys.stderr.write("worklog pre-commit: unmasked sensitive text in:\n  " + "\n  ".join(bad) +
                             "\nRun: python -m chitragupta redact --fix <files>, then re-stage.\n")
            return 1
        return 0
    if mode == "--check":
        bad = [f for f in files if file_needs_fix(f)]
        for f in bad:
            print(f)
        return 1 if bad else 0
    if mode == "--fix":
        for f in files:
            if fix_file(f):
                print("masked:", f)
        return 0
    if mode == "--stdin":
        sys.stdout.write(redact(sys.stdin.read()))
        return 0
    if mode == "--public":
        for f in files:
            if fix_file(f, mode="public"):
                print("masked (public):", f)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
