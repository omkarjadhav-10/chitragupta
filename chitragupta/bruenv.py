"""Variables for replaying a ticket's saved requests outside Bruno (replay.sh and requests.md use it).
Run as `"<python>" "<launcher>" bruenv <cmd> ...` (= `python -m chitragupta bruenv <cmd> ...`).

    bruenv.py shell ENV                       print `export NAME='value'` lines (base URLs + bruno/.env)
    bruenv.py subst ENV VARS TEXT             fill {{var}} / {{process.env.NAME}} in TEXT, print it
    bruenv.py file ENV VARS SRC DST           same, for a file (request bodies)
    bruenv.py body ENV SRC                    fill a body file into a temp file and print its path
    bruenv.py check ENV NAME...               exit 2 listing every process.env NAME that is missing or empty
    bruenv.py setvar VARS NAME VALUE          store a runtime variable (e.g. a token) for later steps
    bruenv.py capture VARS RESP NAME PATH     store a value from a JSON response (PATH like data.accessToken, or auto)

ENV is an environment name from bruno/environments (case-insensitive: qa, QA, local ...).
Non-secret values come from bruno/environments/<ENV>.bru, secrets from bruno/.env. Runtime variables
live only in the temporary VARS file of one replay run; nothing here writes a secret into the repo.
"""
import datetime as _dt
import json
import os
import random
import re
import sys
import tempfile
import uuid

from . import common

HOME = common.home()
BRUNO = common.env("BRUNO_DIR") or os.path.join(HOME, "bruno")
VAR = re.compile(r"\{\{\s*([\w.$-]+)\s*\}\}")
TOKEN_CHAIN = ("token", "accessToken", "access_token", "data.token", "data.accessToken", "data.access_token", "id_token")


class Missing(Exception):
    pass


def env_file(env):
    d = os.path.join(BRUNO, "environments")
    try:
        for fn in os.listdir(d):
            if fn.lower() == (env or "").lower() + ".bru":
                return os.path.join(d, fn)
    except OSError:
        pass
    return None


def env_vars(env):
    from . import brufile
    p = env_file(env)
    if not p:
        names = sorted(fn[:-4] for fn in os.listdir(os.path.join(BRUNO, "environments")) if fn.endswith(".bru")) \
            if os.path.isdir(os.path.join(BRUNO, "environments")) else []
        raise Missing("unknown environment %r (have: %s)" % (env, ", ".join(names) or "none"))
    with open(p, encoding="utf-8") as f:
        vars_, _ = brufile.parse_env(f.read())
    return dict(vars_)


def dotenv():
    out = {}
    try:
        with open(os.path.join(BRUNO, ".env"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                out[k.strip()] = v
    except OSError:
        pass
    return out


def runtime(vars_path):
    try:
        with open(vars_path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return {}


def save_runtime(vars_path, data):
    with open(vars_path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def dynamic(name):
    if name == "$guid" or name == "$randomUUID":
        return str(uuid.uuid4())
    if name == "$timestamp":
        return str(int(_dt.datetime.now().timestamp()))
    if name == "$isoTimestamp":
        return _dt.datetime.now(_dt.timezone.utc).isoformat()
    if name == "$randomInt":
        return str(random.randint(0, 1000))
    return None


def substitute(text, env, vars_path=None):
    envv, dot, rt = env_vars(env), dotenv(), runtime(vars_path) if vars_path else {}
    missing = []

    def repl(m):
        name = m.group(1)
        if name.startswith("process.env."):
            k = name[len("process.env."):]
            v = dot.get(k) or os.environ.get(k)
            if not v:
                missing.append("%s (set it in bruno/.env)" % k)
                return m.group(0)
            return v
        if name.startswith("$"):
            v = dynamic(name)
            return v if v is not None else m.group(0)
        for src in (rt, envv):
            if name in src and src[name] != "":
                return VAR.sub(repl, str(src[name]))  # env values may reference other vars
        v = os.environ.get(name)
        if v:
            return v
        missing.append("%s (environment %s or an earlier step)" % (name, env))
        return m.group(0)

    out = VAR.sub(repl, text)
    if missing:
        raise Missing("missing " + ", ".join(dict.fromkeys(missing)))
    return out


def get_path(obj, path):
    cur = obj
    for p in path.split("."):
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        elif isinstance(cur, list) and p.isdigit() and int(p) < len(cur):
            cur = cur[int(p)]
        else:
            return None
    return cur


def capture(vars_path, resp_path, name, path):
    try:
        with open(resp_path, encoding="utf-8", errors="replace") as f:
            body = json.load(f)
    except (OSError, ValueError):
        return False
    paths = TOKEN_CHAIN if path in ("", "auto") else (path,)
    for p in paths:
        v = get_path(body, p)
        if isinstance(v, (str, int, float)) and str(v):
            rt = runtime(vars_path)
            rt[name] = str(v)
            save_runtime(vars_path, rt)
            return True
    return False


def shell_name(k):
    return re.sub(r"\W", "_", k)


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "shell":
            out = []
            for k, v in list(env_vars(args[0]).items()) + list(dotenv().items()):
                out.append("export %s='%s'" % (shell_name(k), str(v).replace("'", "'\\''")))
            print("\n".join(out))
        elif cmd == "subst":
            sys.stdout.write(substitute(args[2], args[0], args[1]))
        elif cmd == "file":
            with open(args[2], encoding="utf-8") as f:
                txt = substitute(f.read(), args[0], args[1])
            with open(args[3], "w", encoding="utf-8", newline="") as f:
                f.write(txt)
        elif cmd == "body":
            with open(args[1], encoding="utf-8") as f:
                txt = substitute(f.read(), args[0], None)
            fd, p = tempfile.mkstemp(prefix="wl-body-", suffix=os.path.splitext(args[1])[1])
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(txt)
            print(p.replace("\\", "/"))
        elif cmd == "check":
            dot = dotenv()
            miss = [n for n in args[1:] if not (dot.get(n) or os.environ.get(n))]
            env_vars(args[0])
            if miss:
                sys.stderr.write("Fill these in %s first:\n  %s\n" % (os.path.join(BRUNO, ".env"), "\n  ".join(miss)))
                return 2
        elif cmd == "setvar":
            rt = runtime(args[0])
            rt[args[1]] = args[2]
            save_runtime(args[0], rt)
        elif cmd == "capture":
            if not capture(args[0], args[1], args[2], args[3] if len(args) > 3 else "auto"):
                sys.stderr.write("could not find %s in the response\n" % (args[3] if len(args) > 3 else "a token"))
                return 1
        else:
            print(__doc__)
            return 2
    except Missing as e:
        sys.stderr.write("bruenv: %s\n" % e)
        return 3
    except (IndexError, OSError) as e:
        sys.stderr.write("bruenv: %s\n" % e)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
