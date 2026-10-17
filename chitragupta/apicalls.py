"""HTTP calls: parse curl / PowerShell / httpie / wget commands, read the outcome from tool output
(status + key skeleton, never values), and turn a captured call into a replayable, privacy-safe template.

Templating rules:
- base URLs become environment variables ({{ordersBaseUrl}}), learned from bruno/environments and
  the configured Bruno collections; unknown hosts get a new variable added to the matching env file
- credentials, tokens, cookies, API keys -> {{process.env.NAME}} or a runtime variable ({{token}})
- person-identifying values -> ticket-scoped {{process.env.ABC1001_*}} with a synthetic default
- everything else stays literal, especially nulls, booleans, enums and scenario-critical fields
"""
import json
import os
import re
from urllib.parse import urlsplit, parse_qsl

HTTP_CMD = re.compile(r"(?i)(?:^|[\s;|&(`$])(curl(?:\.exe)?|Invoke-RestMethod|Invoke-WebRequest|irm|iwr|wget|https?)(?=\s)")


def is_http_command(cmd):
    if not cmd:
        return False
    m = HTTP_CMD.search(cmd)
    if not m:
        return False
    if m.group(1).lower() in ("http", "https", "irm", "iwr"):  # httpie / aliases need a URL nearby
        return bool(re.search(r"https?://|localhost:|:\d{2,5}/", cmd))
    return True


# --------------------------------------------------------------------------- tokenizer

def split_shell(cmd):
    """Bash/PowerShell-ish tokenizer. Returns list of segments (list of tokens), split on | && || ; and newlines.
    Quotes: '...', "...", $'...', PowerShell here-strings @'...'@ / @"..."@. Line continuations: \\ and `."""
    s = cmd.replace("\r\n", "\n")
    s = re.sub(r"\\\n|`\n|\^\n", " ", s)
    segs, cur, tok, i, n, has = [], [], [], 0, len(s), False

    def end_tok():
        nonlocal tok, has
        if has:
            cur.append("".join(tok))
        tok, has = [], False

    while i < n:
        c = s[i]
        if s.startswith("@'", i) or s.startswith('@"', i):
            q = s[i + 1]
            j = s.find("\n" + q + "@", i + 2)
            if j > 0:
                tok.append(s[i + 2: j].lstrip("\n"))
                has = True
                i = j + 3
                continue
        if c == "'" :
            j = s.find("'", i + 1)
            j = n if j < 0 else j
            tok.append(s[i + 1: j])
            has, i = True, j + 1
            continue
        if c == "$" and i + 1 < n and s[i + 1] == "'":
            j, buf = i + 2, []
            while j < n and s[j] != "'":
                if s[j] == "\\" and j + 1 < n:
                    buf.append({"n": "\n", "t": "\t", "'": "'", "\\": "\\", '"': '"'}.get(s[j + 1], s[j + 1]))
                    j += 2
                    continue
                buf.append(s[j])
                j += 1
            tok.append("".join(buf))
            has, i = True, j + 1
            continue
        if c == '"':
            j, buf = i + 1, []
            while j < n and s[j] != '"':
                if s[j] == "\\" and j + 1 < n and s[j + 1] in '"\\$`':
                    buf.append(s[j + 1])
                    j += 2
                    continue
                if s[j] == "`" and j + 1 < n and s[j + 1] == '"':
                    buf.append('"')
                    j += 2
                    continue
                buf.append(s[j])
                j += 1
            tok.append("".join(buf))
            has, i = True, j + 1
            continue
        if c == "\\" and i + 1 < n:
            tok.append(s[i + 1])
            has, i = True, i + 2
            continue
        if c in " \t":
            end_tok()
            i += 1
            continue
        if c in "\n;" or s.startswith("&&", i) or s.startswith("||", i) or c == "|":
            end_tok()
            if cur:
                segs.append(cur)
            cur = []
            i += 2 if s.startswith(("&&", "||"), i) else 1
            continue
        tok.append(c)
        has = True
        i += 1
    end_tok()
    if cur:
        segs.append(cur)
    return segs


# --------------------------------------------------------------------------- command parsing

_CURL_ARG = {"-X", "--request", "-H", "--header", "-d", "--data", "--data-raw", "--data-binary", "--data-ascii",
             "--data-urlencode", "--json", "-u", "--user", "-b", "--cookie", "-c", "--cookie-jar", "-F", "--form",
             "-o", "--output", "-w", "--write-out", "-m", "--max-time", "--connect-timeout", "-A", "--user-agent",
             "-e", "--referer", "--cacert", "--cert", "--key", "-T", "--upload-file", "--url", "-x", "--proxy",
             "--retry", "-K", "--config", "--resolve", "--oauth2-bearer", "-E", "--form-string", "-D", "--dump-header"}


def parse_curl(tokens, cwd=None, files=None):
    req = {"tool": "curl", "method": None, "url": "", "headers": [], "body": None, "form": [], "basic": None,
           "cookie": False, "write_out": None, "get": False, "body_file": None}
    i, data = 1, []
    while i < len(tokens):
        t = tokens[i]
        val = None
        if t.startswith("--") and "=" in t and t.split("=", 1)[0] in _CURL_ARG:
            t, val = t.split("=", 1)
        elif t in _CURL_ARG:
            val = tokens[i + 1] if i + 1 < len(tokens) else ""
            i += 1
        elif re.match(r"^-[A-Za-z]{2,}$", t) and t[-1] in "XHdubcFowmAeTx" and t[1:-1].isalpha():
            # combined short flags like -sSX POST
            val = tokens[i + 1] if i + 1 < len(tokens) else ""
            t = "-" + t[-1]
            i += 1
        i += 1
        if val is None:
            if t in ("-G", "--get"):
                req["get"] = True
            elif t in ("-I", "--head"):
                req["method"] = req["method"] or "HEAD"
            elif not t.startswith("-") and not req["url"] and (re.match(r"^(https?://|\$|\{\{|localhost|\d+\.\d+)", t) or "/" in t):
                req["url"] = t
            continue
        if t in ("-X", "--request"):
            req["method"] = val.upper()
        elif t in ("-H", "--header"):
            if ":" in val:
                k, v = val.split(":", 1)
                req["headers"].append([k.strip(), v.strip()])
        elif t in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--json", "--data-urlencode"):
            if t == "--json":
                req["headers"].append(["Content-Type", "application/json"])
            if val.startswith("@") and t != "--data-raw":
                req["body_file"] = val[1:]
                content = (files or {}).get(val[1:])
                if content is None:
                    content = _read_file(val[1:], cwd)
                data.append(content if content is not None else "")
            else:
                data.append(val)
        elif t in ("-u", "--user"):
            req["basic"] = val
        elif t in ("-b", "--cookie"):
            req["cookie"] = True
            if "=" in val:
                req["headers"].append(["Cookie", "****"])
        elif t in ("-F", "--form", "--form-string"):
            if "=" in val:
                k, v = val.split("=", 1)
                req["form"].append([k, v])
        elif t in ("-w", "--write-out"):
            req["write_out"] = val
        elif t == "--url":
            req["url"] = val
        elif t == "--oauth2-bearer":
            req["headers"].append(["Authorization", "Bearer " + val])
    if data:
        req["body"] = "&".join(data) if len(data) > 1 else data[0]
    if req["get"] and req["body"]:
        sep = "&" if "?" in req["url"] else "?"
        req["url"], req["body"] = req["url"] + sep + req["body"], None
    if not req["method"]:
        req["method"] = "POST" if (req["body"] is not None or req["form"]) else "GET"
    return req if req["url"] else None


def _read_file(path, cwd):
    try:
        p = path if os.path.isabs(path) else os.path.join(cwd or os.getcwd(), path)
        if os.path.getsize(p) > 200_000:
            return None
        with open(p, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _ps_hashtable(text):
    out = []
    for m in re.finditer(r"[\"']?([\w-]+)[\"']?\s*=\s*(\"[^\"]*\"|'[^']*'|[^;}\n]+)", text or ""):
        out.append([m.group(1), m.group(2).strip().strip("\"'")])
    return out


def parse_powershell(cmd):
    """Best-effort Invoke-RestMethod / Invoke-WebRequest."""
    m = re.search(r"(?i)\b(Invoke-RestMethod|Invoke-WebRequest|irm|iwr)\b(.*)", cmd, re.S)
    if not m:
        return None
    rest = m.group(2)

    def opt(name):
        mm = re.search(r"(?i)-%s\s+(\"[^\"]*\"|'[^']*'|\S+)" % name, rest)
        return mm.group(1).strip("\"'") if mm else None
    url = opt("Uri") or (re.search(r"(https?://\S+)", rest).group(1).strip("\"'") if re.search(r"https?://\S+", rest) else "")
    req = {"tool": "powershell", "method": (opt("Method") or "GET").upper(), "url": url, "headers": [], "body": None,
           "form": [], "basic": None, "cookie": False, "write_out": None, "body_file": None}
    hm = re.search(r"(?i)-Headers\s+@\{([^}]*)\}", rest)
    if hm:
        req["headers"] = _ps_hashtable(hm.group(1))
    else:
        hv = re.search(r"(?i)-Headers\s+\$(\w+)", rest)
        if hv:
            dm = re.search(r"\$%s\s*=\s*@\{([^}]*)\}" % re.escape(hv.group(1)), cmd)
            if dm:
                req["headers"] = _ps_hashtable(dm.group(1))
    ct = opt("ContentType")
    if ct:
        req["headers"].append(["Content-Type", ct])
    bm = re.search(r"(?i)-Body\s+(@'\n?(.*?)\n'@|@\"\n?(.*?)\n\"@|'((?:[^']|'')*)'|\"((?:[^\"`]|`.)*)\"|\$(\w+))", rest, re.S)
    if bm:
        if bm.group(6):
            var = bm.group(6)
            dm = re.search(r"\$%s\s*=\s*(@'\n?(.*?)\n'@|@\"\n?(.*?)\n\"@|'((?:[^']|'')*)')" % re.escape(var), cmd, re.S)
            if dm:
                req["body"] = next(g for g in dm.groups()[1:] if g is not None)
            else:
                hm2 = re.search(r"\$%s\s*=\s*@\{(.*?)\}\s*\|\s*ConvertTo-Json" % re.escape(var), cmd, re.S | re.I)
                if hm2:
                    req["body"] = json.dumps({k: v for k, v in _ps_hashtable(hm2.group(1))})
        else:
            req["body"] = next((g for g in bm.groups()[1:5] if g is not None), None)
            if bm.group(4) is not None:
                req["body"] = bm.group(4).replace("''", "'")
    return req if req["url"] else None


def parse_command(cmd, cwd=None, files=None):
    """All HTTP requests found in one shell command, in order."""
    out = []
    if re.search(r"(?i)\b(Invoke-RestMethod|Invoke-WebRequest)\b", cmd or ""):
        r = parse_powershell(cmd)
        return [r] if r else []
    try:
        segs = split_shell(cmd or "")
    except Exception:
        return []
    shvars = {}

    def sub(tok):
        return re.sub(r"\$\{?([A-Za-z_]\w*)\}?", lambda m: shvars.get(m.group(1), m.group(0)), tok)

    for seg in segs:
        while seg and seg[0] in ("do", "then", "else", "{", "(", "time", "!"):
            seg = seg[1:]
        if len(seg) >= 3 and seg[0] == "for" and seg[2] == "in":
            if len(seg) > 3:
                shvars[seg[1]] = sub(seg[3])  # first loop value stands for the loop
            continue
        # VAR=x assignments (alone, or as prefixes like VAR=x curl ...)
        k = 0
        while k < len(seg) and re.match(r"^[A-Za-z_]\w*=", seg[k]):
            name, val = seg[k].split("=", 1)
            if not val.startswith("$("):
                shvars[name] = sub(val)
            k += 1
        seg = [sub(t) for t in seg[k:]]
        if not seg:
            continue
        head = os.path.basename(seg[0]).lower()
        if head in ("curl", "curl.exe"):
            r = parse_curl(seg, cwd, files)
            if r:
                out.append(r)
        elif head in ("http", "https") and len(seg) > 1:
            meth = seg[1].upper() if seg[1].upper() in ("GET", "POST", "PUT", "PATCH", "DELETE") else None
            rest = seg[2:] if meth else seg[1:]
            if rest:
                url = rest[0] if "://" in rest[0] else ("http://" + rest[0] if head == "http" else "https://" + rest[0])
                hdr = [[a.split(":", 1)[0], a.split(":", 1)[1]] for a in rest[1:] if re.match(r"^[\w-]+:[^=]", a)]
                body = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in rest[1:] if re.match(r"^[\w.-]+=[^=]", a)}
                bodyj = {a.split(":=", 1)[0]: _maybe_json(a.split(":=", 1)[1]) for a in rest[1:] if ":=" in a}
                body.update(bodyj)
                out.append({"tool": "httpie", "method": meth or ("POST" if body else "GET"), "url": url, "headers": hdr,
                            "body": json.dumps(body) if body else None, "form": [], "basic": None, "cookie": False,
                            "write_out": None, "body_file": None})
        elif head in ("wget", "wget.exe"):
            url = next((t for t in seg[1:] if "://" in t), "")
            meth = next((t.split("=", 1)[1].upper() for t in seg if t.startswith("--method=")), None)
            body = next((t.split("=", 1)[1] for t in seg if t.startswith(("--body-data=", "--post-data="))), None)
            hdr = [t.split("=", 1)[1].split(":", 1) for t in seg if t.startswith("--header=") and ":" in t]
            if url:
                out.append({"tool": "wget", "method": meth or ("POST" if body else "GET"), "url": url,
                            "headers": [[a.strip(), b.strip()] for a, b in hdr], "body": body, "form": [], "basic": None,
                            "cookie": False, "write_out": None, "body_file": None})
    return out


def _maybe_json(s):
    try:
        return json.loads(s)
    except ValueError:
        return s


# --------------------------------------------------------------------------- tool output -> outcome

def response_text(tool_response):
    """Plain text of a Bash/PowerShell tool_response (dict or string)."""
    if tool_response is None:
        return ""
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, dict):
        parts = []
        for k in ("stdout", "output", "result", "content", "stderr"):
            v = tool_response.get(k)
            if isinstance(v, str):
                parts.append(v)
            elif isinstance(v, list):
                parts += [x.get("text", "") if isinstance(x, dict) else str(x) for x in v]
        return "\n".join(parts)
    if isinstance(tool_response, list):
        return "\n".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in tool_response)
    return str(tool_response)


def statuses(output, reqs=None):
    """HTTP status codes found in command output (in order)."""
    out = [int(x) for x in re.findall(r"(?m)^[<\s]*HTTP/[\d.]+\s+(\d{3})\b", output or "") if not x.startswith("1")]
    if out:
        return out
    for r in reqs or []:
        wo = r.get("write_out")
        if wo and "%{http_code}" in wo:
            prefix = re.split(r"\\n|\n|%\{[a-z_]+\}", wo.split("%{http_code}")[0])[-1]
            pat = (re.escape(prefix) + r"(\d{3})\b") if prefix.strip() else r"(?m)^\s*(\d{3})\b"
            found = [int(x) for x in re.findall(pat, output or "")]
            if found:
                return found
    pats = [r"(?i)\b(?:http[_ ]?(?:code|status)|status[_ ]?code|status)\s*[:=]\s*(\d{3})\b",
            r"(?i)Response status code does not indicate success:\s*(\d{3})",
            r"(?i)\bStatusCode\s*:\s*(\d{3})\b"]
    for p in pats:
        found = [int(x) for x in re.findall(p, output or "")]
        if found:
            return found
    m = re.search(r"(?:^|\n)\s*(\d{3})\s*$", output or "")
    if m and any(r.get("write_out") for r in reqs or []):
        return [int(m.group(1))]
    return []


def _json_candidates(text):
    dec = json.JSONDecoder()
    found, i = [], 0
    text = text or ""
    while i < len(text) and len(found) < 20:
        j = min([k for k in (text.find("{", i), text.find("[", i)) if k >= 0], default=-1)
        if j < 0:
            break
        try:
            obj, end = dec.raw_decode(text, j)
            if isinstance(obj, (dict, list)) and (obj or end - j > 2):
                found.append((end - j, obj))
                i = end
                continue
        except ValueError:
            pass
        i = j + 1
    return [o for _, o in sorted(found, key=lambda x: -x[0])]


def skeleton(obj, depth=0, max_depth=4):
    """Keys and value types only (first array element), never values. Keys that look like data are masked."""
    if isinstance(obj, dict):
        if depth >= max_depth:
            return "object"
        out = {}
        for k in list(obj)[:40]:
            kk = k
            if re.search(r"@|\d{4,}", str(k)):
                kk = re.sub(r"[A-Za-z]", "x", re.sub(r"\d", "X", str(k)))
            out[kk] = skeleton(obj[k], depth + 1, max_depth)
        if len(obj) > 40:
            out["…"] = "%d more keys" % (len(obj) - 40)
        return out
    if isinstance(obj, list):
        if depth >= max_depth:
            return "array"
        return [skeleton(obj[0], depth + 1, max_depth)] if obj else []
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        return "boolean"
    if isinstance(obj, (int, float)):
        return "number"
    return "string"


def response_keys(output):
    c = _json_candidates(output)
    return skeleton(c[0]) if c else None


def find_token_path(skel, prefix=""):
    """Dotted path of a token-like string field in a response skeleton."""
    if not isinstance(skel, dict):
        return None
    names = ("access_token", "accessToken", "token", "id_token", "idToken", "jwt", "authToken", "auth_token")
    for n in names:
        if skel.get(n) == "string":
            return prefix + n
    for k, v in skel.items():
        if isinstance(v, dict):
            p = find_token_path(v, prefix + k + ".")
            if p:
                return p
    return None


def outcome_meta(cmd, tool_response, reqs=None):
    out = response_text(tool_response)
    st = statuses(out, reqs)
    keys = response_keys(out)
    err = bool(re.search(r"curl: \(\d+\)|Could not resolve host|Failed to connect|Connection refused|timed out", out))
    return {"statuses": st[:10], "response_keys": keys, "token_path": find_token_path(keys) if keys else None,
            "transport_error": err}


def outcome_of(status, transport_error=False):
    if transport_error and not status:
        return "failed"
    if status is None:
        return "unknown"
    return "worked" if 200 <= status < 400 else "failed"


# --------------------------------------------------------------------------- templating

PERSON_RX = re.compile(
    r"^(first_?name|last_?name|middle_?name|full_?name|preferred_?name|patient_?name|given_?name|family_?name|given|family|"
    r"birth_?date|date_?of_?birth|dob|mrn|medical_?record_?(?:number|no)|ssn|social_?security(?:_?number)?|"
    r"phone(?:_?number)?|mobile(?:_?number)?|home_?phone|cell_?phone|work_?phone|fax|email(?:_?address)?|"
    r"address(?:_?line)?_?[12]?|street(?:_?address)?|city|postal_?code|zip(?:_?code)?|"
    r"member_?id|subscriber_?id|policy_?number|accession(?:_?(?:number|no|id))?|patient_?id|npi)$", re.I)
SECRET_RX = re.compile(r"(?i)^(password|passwd|pwd|passphrase|secret|client_?secret|api_?key|apikey|access_?key|"
                       r"private_?key|token|access_?token|refresh_?token|id_?token|auth_?token|otp|pin)$")
LOGIN_ID_RX = re.compile(r"(?i)^(email|username|user_?name|login|user)$")
EMAIL_VAL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
PHONE_VAL = re.compile(r"^\+?[\d\s().-]{10,16}$")
MASKED_VAL = re.compile(r"^(?=.*[xX])[xX\d\W_]*$")  # shape-masked by redact(): xxxx@xxxx.xxx, XXX-XX-XXXX
DROP_HEADERS = {"user-agent", "accept-encoding", "content-length", "host", "connection", "postman-token",
                "cache-control", "pragma", "referer", "accept-language", "origin", "x-request-id", "x-correlation-id",
                "upgrade-insecure-requests", "dnt", "te", "priority"}


def snake(k):
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", str(k))
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()


def synthetic_default(key):
    k = snake(key)
    table = [
        (r"first|given|preferred", "Test"), (r"last|family", "Patient"), (r"middle", "Q"), (r"full|patient_name|name", "Test Patient"),
        (r"birth|dob", "1990-01-01"), (r"mrn|medical_record", "MRN0000001"), (r"ssn|social", "000000000"),
        (r"phone|mobile|fax|contact_phone", "5555550100"), (r"email|contact_email", "test.patient@example.com"),
        (r"address|street", "1 Test Street"), (r"city", "Testville"), (r"postal|zip", "77001"),
        (r"member|subscriber", "MEM0000001"), (r"policy", "POL0000001"), (r"accession", "ACC0000001"),
        (r"patient_id", "PAT0000001"), (r"npi", "1234567893"),
    ]
    for rx, v in table:
        if re.search(rx, k):
            return v
    return "TEST"


def ticket_prefix(ticket):
    return re.sub(r"[^A-Z0-9]", "", (ticket or "WORKLOG").upper())


class Vars:
    """Collects variables a templated request needs: kind env | secret | credential | person | runtime."""

    def __init__(self, ticket):
        self.ticket = ticket
        self.items = {}

    def add(self, name, kind, default="", note=""):
        cur = self.items.get(name)
        if not cur:
            self.items[name] = {"name": name, "kind": kind, "default": default, "note": note}
        return name

    def env_name(self, key):
        return "%s_%s" % (ticket_prefix(self.ticket), snake(key).upper())

    def list(self):
        return sorted(self.items.values(), key=lambda v: (v["kind"], v["name"]))


def template_json(obj, vs, critical=()):
    """Walk a JSON body: secrets/credentials/person values -> {{process.env.*}}. Returns new object."""
    used = {}

    def uniq(name):
        n = used.get(name, 0)
        used[name] = n + 1
        return name if n == 0 else "%s_%d" % (name, n + 1)

    def walk(o, parent=None):
        if isinstance(o, list):
            return [walk(x, o) for x in o]
        if not isinstance(o, dict):
            return o
        is_login = any(SECRET_RX.match(str(k)) for k in o) and any(LOGIN_ID_RX.match(str(k)) for k in o)
        ctype = str(o.get("contactType") or o.get("type") or o.get("system") or "").upper()
        out = {}
        for k, v in o.items():
            if not isinstance(v, (str, int, float)) or isinstance(v, bool):
                out[k] = walk(v, o) if isinstance(v, (dict, list)) else v
                continue
            sv = str(v)
            sensitive = SECRET_RX.match(str(k)) or LOGIN_ID_RX.match(str(k)) or PERSON_RX.match(snake(k)) or \
                PERSON_RX.match(str(k)) or MASKED_VAL.match(sv) or EMAIL_VAL.match(sv)
            if k in critical and not sensitive:  # scenario-critical values stay literal; privacy always wins
                out[k] = v
                continue
            if sv.startswith("{{") and sv.endswith("}}"):
                out[k] = v
            elif SECRET_RX.match(str(k)):
                n = uniq(vs.env_name(k))
                vs.add(n, "credential" if str(k).lower() in ("password", "passwd", "pwd", "passphrase", "pin", "otp") else "secret")
                out[k] = "{{process.env.%s}}" % n
            elif is_login and LOGIN_ID_RX.match(str(k)):
                n = uniq(vs.env_name(k))
                vs.add(n, "credential", note="login %s" % k)
                out[k] = "{{process.env.%s}}" % n
            elif PERSON_RX.match(snake(k)) or PERSON_RX.match(str(k)):
                n = uniq(vs.env_name(k))
                vs.add(n, "person", synthetic_default(k))
                out[k] = "{{process.env.%s}}" % n
            elif str(k).lower() in ("contactvalue", "value", "telecom", "contact") and isinstance(v, str) and \
                    (ctype in ("EMAIL", "PHONE", "FAX", "SMS", "MOBILE") or EMAIL_VAL.match(sv) or PHONE_VAL.match(sv) or MASKED_VAL.match(sv)):
                kind = ctype.lower() or ("email" if EMAIL_VAL.match(sv) or "@" in sv else "phone")
                n = uniq(vs.env_name("contact_" + kind))
                vs.add(n, "person", synthetic_default(kind))
                out[k] = "{{process.env.%s}}" % n
            elif isinstance(v, str) and (EMAIL_VAL.match(sv) or (MASKED_VAL.match(sv) and "@" in sv)):
                n = uniq(vs.env_name(k))
                vs.add(n, "person", synthetic_default("email"))
                out[k] = "{{process.env.%s}}" % n
            elif isinstance(v, str) and MASKED_VAL.match(sv) and len(sv) >= 4:
                n = uniq(vs.env_name(k))  # a value redact() already masked: never replay a mask
                vs.add(n, "person", synthetic_default(k))
                out[k] = "{{process.env.%s}}" % n
            else:
                out[k] = v
        return out

    return walk(obj)


def template_body_text(body, vs, critical=()):
    """Returns (body_text, mode). JSON bodies are re-serialized; JSON null stays null."""
    if body is None:
        return None, "none"
    s = body.strip()
    if s[:1] in "{[":
        try:
            obj = json.loads(s)
            return json.dumps(template_json(obj, vs, critical), indent=2, ensure_ascii=False), "json"
        except ValueError:
            pass
    if re.match(r"^[\w.%-]+=[^&]*(&[\w.%-]+=[^&]*)*$", s):
        pairs = parse_qsl(s, keep_blank_values=True)
        out = []
        for k, v in pairs:
            tj = template_json({k: v}, vs, critical)
            out.append([k, tj[k]])
        return out, "formUrlEncoded"
    return s, "text"


class EnvIndex:
    """Base-URL variables per environment, learned from bruno/environments plus the configured collections."""

    ENV_HINTS = (("LOCAL", r"localhost|127\.0\.0\.1|0\.0\.0\.0|host\.docker"), ("UAT", r"(?:^|[.-])uat(?:[.-]|$)"),
                 ("STG", r"(?:^|[.-])(?:stg|stage|staging)(?:[.-]|$)"), ("DEV", r"(?:^|[.-])dev(?:[.-]|$)"),
                 ("QA", r"(?:^|[.-])qa\d?(?:[.-]|$)"), ("PROD", r"(?:^|[.-])prod(?:[.-]|$)"))

    def __init__(self, own_envs, learned_envs=None, url_prefixes=None):
        self.own = own_envs or {}          # {ENV: {var: value}}
        self.learned = learned_envs or {}  # {ENV: {var: value}} url-valued vars only
        self.prefixes = url_prefixes or {}  # {"var|seg/seg": count}
        self.additions = {}                # {ENV: {var: value}}

    @staticmethod
    def env_of_host(host):
        for env, rx in EnvIndex.ENV_HINTS:
            if re.search(rx, host or "", re.I):
                return env
        return "QA"

    def _all(self):
        for src in (self.own, self.additions, self.learned):
            for env, vs in src.items():
                for k, v in vs.items():
                    if isinstance(v, str) and re.match(r"^https?://", v):
                        yield env.upper(), k, v.rstrip("/")

    def resolve(self, url):
        """(templated_url, var or None, env or None)."""
        if not re.match(r"^https?://", url or ""):
            return url, None, None
        cands = []
        for env, k, v in self._all():
            if url == v or url.startswith(v + "/") or url.startswith(v + "?"):
                cands.append((len(v), env, k, v))
        if cands:
            best = max(c[0] for c in cands)
            top = [c for c in cands if c[0] == best]
            rest_path = url[best:]
            segs = "/".join([p for p in urlsplit("http://x" + rest_path.split("?")[0]).path.split("/") if p][:3])

            def score(c):
                own = c[2] in {k for vs in self.own.values() for k in vs}
                hits = sum(n for key, n in self.prefixes.items() if key.startswith(c[2] + "|") and segs.startswith(key.split("|", 1)[1]))
                return (hits, own, -len(c[2]))
            pick = max(top, key=score)
            env, var = pick[1], pick[2]
            self._ensure(env, var, pick[3])
            return "{{%s}}%s" % (var, url[best:]), var, env
        parts = urlsplit(url)
        host = parts.netloc
        env = self.env_of_host(host)
        var = self._new_var(host, parts.path)
        base = "%s://%s" % (parts.scheme, host)
        self.additions.setdefault(env, {})[var] = base
        return "{{%s}}%s" % (var, url[len(base):]), var, env

    def _ensure(self, env, var, value):
        if var not in self.own.get(env, {}):
            self.additions.setdefault(env, {}).setdefault(var, value)
            # copy the same variable's URL for other envs the learned collections know (never secrets)
            for e2, vs in self.learned.items():
                if e2 != env and var in vs and e2 in self.own and var not in self.own[e2]:
                    self.additions.setdefault(e2, {}).setdefault(var, vs[var])

    def _new_var(self, host, path):
        h = re.sub(r":\d+$", "", host).split(".")[0]
        words = [w for w in re.split(r"[-_]", h) if w and w.lower() not in ("qa", "uat", "dev", "stg", "stage", "staging", "prod", "api", "www")
                 and not re.fullmatch(r"v\d+api", w.lower())]  # versioned API host labels (v1api, v2api ...)
        if host.startswith(("localhost", "127.", "0.0.0.0")):
            port = host.rsplit(":", 1)[1] if ":" in host else "80"
            words = ["local" + port]
        name = (words[0].lower() + "".join(w.capitalize() for w in words[1:])) if words else "api"
        name = re.sub(r"[^A-Za-z0-9]", "", name) + "BaseUrl"
        taken = {k for _, k, _ in self._all()}
        base, n = name, 2
        while name in taken:
            name = "%s%d" % (base[:-7], n) + "BaseUrl"
            n += 1
        return name


def api_key_var(base_var, header):
    stem = re.sub(r"BaseUrl$", "", base_var or "") or re.sub(r"[^A-Za-z0-9]", "", header)
    return re.sub(r"[^A-Z0-9]", "", stem.upper()) + "_API_KEY"


def template_call(call, env_index, critical=()):
    """Captured call -> templated request dict (brufile request shape) + variables + auth mechanism."""
    ticket = call.get("ticket")
    vs = Vars(ticket)
    url = call.get("url") or ""
    query = []
    base_url, q = (url.split("?", 1) + [""])[:2]
    turl, var, env = env_index.resolve(base_url)
    turl = re.sub(r"\$\{?(\w+)\}?", lambda m: "{{%s}}" % m.group(1), turl)  # shell variables -> runtime vars
    if q:
        for k, v in parse_qsl(q, keep_blank_values=True):
            if k in critical:
                query.append([k, v])
                continue
            tj = template_json({k: v}, vs)
            query.append([k, tj[k]])
        turl += "?" + "&".join("%s=%s" % (k, v) for k, v in query)
    headers, auth, mech = [], {"mode": "none"}, "none"
    for k, v in call.get("headers") or []:
        kl = k.lower().strip()
        if kl in DROP_HEADERS or kl.startswith("sec-"):
            continue
        if kl == "authorization":
            m = re.match(r"(?i)^\s*(bearer|basic|token|api-key|apikey|key)\s+(.*)$", v or "")
            scheme = (m.group(1).lower() if m else "")
            if scheme == "bearer":
                auth, mech = {"mode": "bearer", "token": "{{token}}"}, "bearer"
                vs.add("token", "runtime", note="set by the auth step")
            elif scheme == "basic":
                u, p = vs.env_name("username"), vs.env_name("password")
                vs.add(u, "credential")
                vs.add(p, "credential")
                auth, mech = {"mode": "basic", "username": "{{process.env.%s}}" % u, "password": "{{process.env.%s}}" % p}, "basic"
            else:
                n = api_key_var(var, k)
                vs.add(n, "secret", note="API key")
                auth = {"mode": "apikey", "key": k, "value": "%s{{process.env.%s}}" % ((m.group(1) + " ") if m else "", n),
                        "placement": "header"}
                mech = "api_key"
            continue
        if kl in ("x-api-key", "api-key", "apikey", "ocp-apim-subscription-key", "x-auth-token", "x-functions-key"):
            n = api_key_var(var, k)
            vs.add(n, "secret", note="API key")
            auth, mech = {"mode": "apikey", "key": k, "value": "{{process.env.%s}}" % n, "placement": "header"}, "api_key"
            continue
        if kl == "cookie":
            mech = "cookie"
            continue
        headers.append([k, v])
    if call.get("basic"):
        u, p = vs.env_name("username"), vs.env_name("password")
        vs.add(u, "credential")
        vs.add(p, "credential")
        auth, mech = {"mode": "basic", "username": "{{process.env.%s}}" % u, "password": "{{process.env.%s}}" % p}, "basic"
    if call.get("cookie"):
        mech = "cookie"
    body, mode = template_body_text(call.get("body"), vs, critical)
    form, body_text = [], None
    if mode == "formUrlEncoded":
        form, body_text = body, None
    elif mode in ("json", "text"):
        body_text = body
    if call.get("form"):
        mode = "multipartForm"
        for k, v in call["form"]:
            if v.startswith("@"):
                form.append([k, "@file(%s)" % v[1:].split(";")[0]])
            else:
                tj = template_json({k: v}, vs, critical)
                form.append([k, tj[k]])
    known = {k for src in (env_index.own, env_index.additions) for e in src.values() for k in e}
    for m in re.finditer(r"\{\{(?!process\.env\.)([\w.-]+)\}\}", json.dumps([turl, headers, body_text, form, auth])):
        if m.group(1) not in known:
            vs.add(m.group(1), "runtime")
    req = {"method": (call.get("method") or "GET").upper(), "url": turl, "headers": headers, "query": query,
           "auth": auth, "body_mode": mode if (body_text is not None or form) else "none", "body": body_text,
           "form": form, "form_kind": "multipart-form" if mode == "multipartForm" else ("form-urlencoded" if form else None),
           "body_kind": "json" if mode == "json" else ("text" if mode == "text" else None)}
    return req, vs.list(), mech, env


def signature(req):
    """Same request purpose ~ same method + url shape + body keys."""
    u = re.sub(r"\?.*$", "", req.get("url") or "")
    keys = ""
    try:
        b = json.loads(req.get("body") or "null")
        if isinstance(b, dict):
            keys = ",".join(sorted(b))
    except (ValueError, TypeError):
        pass
    return "%s %s %s" % (req.get("method"), u, keys)


def path_shape(url):
    u = re.sub(r"^\{\{[^}]+\}\}", "", re.sub(r"^https?://[^/]+", "", url or ""))
    u = re.sub(r"\?.*$", "", u)
    u = re.sub(r"\{\{[^}]+\}\}|:\w+|\{\w+\}|\d+|[0-9a-fA-F-]{16,}", "*", u)
    return u.rstrip("/").lower()
