"""Parse and write Bruno .bru files (requests, folder.bru, environments). Stdlib only.

Format reference: https://docs.usebruno.com (Bru lang). A file is a list of top-level blocks:
`name {` ... `}` (dictionary or text) or `name [` ... `]` (list), each closed at column 0,
with contents indented two spaces.
"""
import json
import re

METHODS = ("get", "post", "put", "delete", "patch", "options", "head", "connect", "trace")
# body mode (as written in the method block) -> body block name
FORM_BLOCK = {"multipartForm": "multipart-form", "formUrlEncoded": "form-urlencoded"}
TEXT_BLOCKS = ("body:json", "body:text", "body:xml", "body:sparql", "body:graphql", "body:graphql:vars",
               "script:pre-request", "script:post-response", "tests", "docs")
_HEAD = re.compile(r"^([A-Za-z][\w:.\-]*)\s*([\[{])\s*$")


def parse_blocks(text):
    """[(name, raw_lines)] in file order. raw_lines have the two-space indent removed."""
    out, cur, close = [], None, None
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        if cur is None:
            m = _HEAD.match(line)
            if m:
                cur, close = (m.group(1), []), "}" if m.group(2) == "{" else "]"
            continue
        if line.rstrip() == close:
            out.append(cur)
            cur = None
            continue
        cur[1].append(line[2:] if line.startswith("  ") else line.lstrip("\t"))
    if cur is not None:
        out.append(cur)
    return out


def _pairs(lines):
    """Dictionary block -> [(key, value, enabled)]."""
    out = []
    for l in lines:
        if not l.strip():
            continue
        m = re.match(r"^(~?)([^:]+?):\s?(.*)$", l)
        if m:
            out.append((m.group(2).strip(), m.group(3).rstrip(), not m.group(1)))
    return out


def _text(lines):
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    while lines and not lines[0].strip():
        lines = lines[1:]
    return "\n".join(lines)


def parse_request(text):
    """Request dict (None when the file is not an http request, e.g. folder.bru)."""
    blocks = parse_blocks(text)
    req = {"name": "", "seq": None, "method": None, "url": "", "body_mode": "none", "auth": {"mode": "none"},
           "headers": [], "query": [], "body": None, "form": [], "vars_pre": [], "vars_post": [],
           "script_pre": "", "script_post": "", "asserts": [], "docs": "", "tests": ""}
    for name, lines in blocks:
        if name == "meta":
            for k, v, _ in _pairs(lines):
                if k == "name":
                    req["name"] = v
                elif k == "seq":
                    try:
                        req["seq"] = int(v)
                    except ValueError:
                        pass
        elif name in METHODS:
            req["method"] = name.upper()
            for k, v, _ in _pairs(lines):
                if k == "url":
                    req["url"] = v
                elif k == "body":
                    req["body_mode"] = v
                elif k == "auth":
                    req["auth"]["mode"] = v
        elif name == "headers":
            req["headers"] = [[k, v] for k, v, on in _pairs(lines) if on]
        elif name in ("params:query", "query"):
            req["query"] = [[k, v] for k, v, on in _pairs(lines) if on]
        elif name.startswith("auth:"):
            req["auth"].update({k: v for k, v, _ in _pairs(lines)})
            req["auth"]["mode"] = req["auth"].get("mode") if req["auth"].get("mode") not in (None, "none") else name[5:]
        elif name in ("body:json", "body:text", "body:xml"):
            req["body"], req["body_kind"] = _text(lines), name[5:]
        elif name in ("body:form-urlencoded", "body:multipart-form"):
            req["form"], req["form_kind"] = [[k, v] for k, v, on in _pairs(lines) if on], name[5:]
        elif name == "vars:pre-request":
            req["vars_pre"] = [[k, v] for k, v, on in _pairs(lines) if on]
        elif name == "vars:post-response":
            req["vars_post"] = [[k, v] for k, v, on in _pairs(lines) if on]
        elif name == "script:pre-request":
            req["script_pre"] = _text(lines)
        elif name == "script:post-response":
            req["script_post"] = _text(lines)
        elif name == "assert":
            req["asserts"] = [[k, v] for k, v, on in _pairs(lines) if on]
        elif name == "docs":
            req["docs"] = _text(lines)
        elif name == "tests":
            req["tests"] = _text(lines)
    if not req["method"]:
        return None
    req["capture"] = script_captures(req["script_post"]) + [
        {"var": k, "path": re.sub(r"^res\.body\.?", "", v), "how": "vars"} for k, v in req["vars_post"]]
    return req


def parse_folder(text):
    meta, docs = {}, ""
    for name, lines in parse_blocks(text):
        if name == "meta":
            meta = {k: v for k, v, _ in _pairs(lines)}
        elif name == "docs":
            docs = _text(lines)
    return {"name": meta.get("name", ""), "seq": meta.get("seq"), "docs": docs}


_SETVAR = re.compile(r"bru\.set(?:Env)?Var\(\s*[\"'](\w+)[\"']\s*,\s*([^;\n]+?)\)\s*;?\s*$", re.M)


def script_captures(script):
    """Variables a post-response script stores, with the response path when it is a plain property chain."""
    out = []
    for m in _SETVAR.finditer(script or ""):
        expr = m.group(2).strip()
        pm = re.match(r"^(?:res\.getBody\(\)|res\.body|body|b|data|json)\??\.([\w.?]+)$", expr)
        out.append({"var": m.group(1), "path": pm.group(1).replace("?", "") if pm else "auto", "how": "script"})
    return out


def parse_env(text):
    """Environment file -> (vars [(k, v)], secret names [..])."""
    vars_, secrets = [], []
    for name, lines in parse_blocks(text):
        if name == "vars":
            vars_ = [(k, v) for k, v, on in _pairs(lines) if on]
        elif name == "vars:secret":
            for l in lines:
                for part in l.split(","):
                    if part.strip():
                        secrets.append(part.strip().lstrip("~"))
    return vars_, secrets


def render_env(vars_, secrets):
    L = ["vars {"] + ["  %s: %s" % (k, v) for k, v in vars_] + ["}"]
    if secrets:
        L += ["vars:secret ["] + ["  %s%s" % (s, "," if i < len(secrets) - 1 else "") for i, s in enumerate(secrets)] + ["]"]
    return "\n".join(L) + "\n"


def _indent(text, n=2):
    pad = " " * n
    return "\n".join((pad + l) if l.strip() else "" for l in (text or "").split("\n"))


def render_request(req):
    """Request dict -> .bru text (block order follows what Bruno itself writes)."""
    method = (req.get("method") or "GET").lower()
    body_mode = req.get("body_mode") or "none"
    auth = req.get("auth") or {"mode": "none"}
    mode = auth.get("mode") or "none"
    L = ["meta {", "  name: %s" % req.get("name", "request"), "  type: http", "  seq: %s" % (req.get("seq") or 1), "}", "",
         "%s {" % method, "  url: %s" % req.get("url", ""), "  body: %s" % body_mode, "  auth: %s" % mode, "}", ""]
    if req.get("query"):
        L += ["params:query {"] + ["  %s: %s" % (k, v) for k, v in req["query"]] + ["}", ""]
    if req.get("headers"):
        L += ["headers {"] + ["  %s: %s" % (k, v) for k, v in req["headers"]] + ["}", ""]
    if mode == "bearer":
        L += ["auth:bearer {", "  token: %s" % auth.get("token", "{{token}}"), "}", ""]
    elif mode == "basic":
        L += ["auth:basic {", "  username: %s" % auth.get("username", ""), "  password: %s" % auth.get("password", ""), "}", ""]
    elif mode == "apikey":
        L += ["auth:apikey {", "  key: %s" % auth.get("key", ""), "  value: %s" % auth.get("value", ""),
              "  placement: %s" % auth.get("placement", "header"), "}", ""]
    if req.get("body") is not None:
        kind = req.get("body_kind") or (body_mode if body_mode in ("json", "text", "xml") else "json")
        L += ["body:%s {" % kind, _indent(req["body"]), "}", ""]
    if req.get("form"):
        kind = req.get("form_kind") or FORM_BLOCK.get(body_mode, "form-urlencoded")
        L += ["body:%s {" % kind] + ["  %s: %s" % (k, v) for k, v in req["form"]] + ["}", ""]
    if req.get("vars_pre"):
        L += ["vars:pre-request {"] + ["  %s: %s" % (k, v) for k, v in req["vars_pre"]] + ["}", ""]
    if req.get("script_post"):
        L += ["script:post-response {", _indent(req["script_post"]), "}", ""]
    if req.get("asserts"):
        L += ["assert {"] + ["  %s: %s" % (k, v) for k, v in req["asserts"]] + ["}", ""]
    if req.get("docs"):
        L += ["docs {", _indent(req["docs"]), "}", ""]
    return "\n".join(L).rstrip("\n") + "\n"


def render_folder(name, seq, docs):
    L = ["meta {", "  name: %s" % name, "  seq: %s" % (seq or 1), "}", ""]
    if docs:
        L += ["docs {", _indent(docs), "}"]
    return "\n".join(L).rstrip("\n") + "\n"


TOKEN_KEYS = ("access_token", "accessToken", "token", "id_token", "idToken", "jwt", "authToken")


def token_script(var, path):
    """post-response script storing a token as a runtime variable (bru.setVar: never written to files)."""
    if path and path != "auto":
        chain = "b." + path.split(".")[0] + "".join("?.%s" % p for p in path.split(".")[1:])
        return 'const b = res.getBody() || {};\nconst t = %s;\nif (t) bru.setVar("%s", t);' % (chain, var)
    return ('const b = res.getBody() || {};\n'
            'const t = b.token || b.accessToken || b.access_token || (b.data && (b.data.token || b.data.accessToken || b.data.access_token));\n'
            'if (t) bru.setVar("%s", t);' % var)


def json_body_text(obj):
    return json.dumps(obj, indent=2, ensure_ascii=False)
