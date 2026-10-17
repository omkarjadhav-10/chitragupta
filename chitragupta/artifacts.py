"""Artifacts filed under their ticket: analysis docs, HTML pages, diagrams, payloads, queries, requests,
investigation scripts, exports, screenshots/documents, published links.

Sources: Write/Edit events (content captured at write time, so scratchpads that get cleaned up are covered),
untracked repo files after a session goes idle/ends, published artifact links, Worklog/_drop (moved) and
configured watch folders (copied), plus files you put straight into tickets/<T>/.

Storage under tickets/<T>/artifacts/:
  <date>_<name>            redacted text copy (pushed); HTML without embedded data: images
  <date>_<name>.summary.md CSV/XLSX: header row + row count only (data rows never pushed)
  local/                   git-ignored: binaries, originals that can't be safely redacted, older binary versions
  INDEX.md                 one row per artifact
"""
import csv
import hashlib
import io
import os
import re
import shutil
import zipfile

from . import common
from . import gitro
from .redact import redact

TEXT_EXT = {".md": "doc", ".markdown": "doc", ".txt": "doc", ".html": "html", ".htm": "html", ".mmd": "diagram",
            ".mermaid": "diagram", ".puml": "diagram", ".plantuml": "diagram", ".svg": "diagram", ".drawio": "diagram",
            ".sql": "query", ".bru": "request", ".http": "request", ".rest": "request", ".json": "payload",
            ".yaml": "payload", ".yml": "payload", ".xml": "payload"}
SCRIPT_EXT = {".py": "script", ".ps1": "script", ".sh": "script", ".js": "script", ".mjs": "script", ".ts": "script"}
EXPORT_EXT = {".csv": "export", ".tsv": "export", ".xlsx": "export"}
BINARY_EXT = {".png": "screenshot", ".jpg": "screenshot", ".jpeg": "screenshot", ".gif": "screenshot", ".webp": "screenshot",
              ".pdf": "document", ".docx": "document", ".pptx": "document", ".xlsx": "export"}
ALL_EXT = set(TEXT_EXT) | set(SCRIPT_EXT) | set(EXPORT_EXT) | set(BINARY_EXT)
SKIP_PARTS = {"target", "bin", "obj", "node_modules", "dist", "build", ".idea", ".vs", ".git", ".gradle", "__pycache__", ".venv", "venv"}
GENERATED_IN_TICKET = {"README.md", "queries.md", "requests.md", "replay.sh", "db-flow.md"}


def kind_of(path):
    e = os.path.splitext(path)[1].lower()
    return TEXT_EXT.get(e) or SCRIPT_EXT.get(e) or EXPORT_EXT.get(e) or BINARY_EXT.get(e)


def is_text(path):
    e = os.path.splitext(path)[1].lower()
    return e in TEXT_EXT or e in SCRIPT_EXT or e in (".csv", ".tsv")


def excluded(path, cfg=None):
    """Not an artifact: build/vendor/IDE folders, temp/swap files, the Worklog folder, Claude's own config/memory."""
    if not path:
        return True
    p = path.replace("\\", "/")
    pl = p.lower()
    if common.path_within(p, common.home()):
        return True
    parts = set(pl.split("/"))
    if parts & SKIP_PARTS:
        return True
    base = os.path.basename(pl)
    if base.endswith(("~", ".swp", ".swo", ".tmp", ".bak", ".orig", ".lock")) or base.startswith((".~", "~$", ".#")):
        return True
    claude = os.path.join(os.path.expanduser("~"), ".claude").replace("\\", "/").lower()
    if pl.startswith(claude + "/") and not pl.startswith(claude + "/plans/"):
        return True
    if os.path.splitext(pl)[1] not in ALL_EXT:
        return True
    if pl.endswith(".bru") and "/environments/" in pl:
        return True  # Bruno environment files are configuration, not artifacts
    return False


def file_sha(path):
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def strip_data_uris(html):
    return re.sub(r"""(src|href)\s*=\s*(["'])data:[^"']{40,}\2""", r'\1=\2\2 data-worklog="embedded data removed"', html or "", flags=re.I)


def title_of(text, ext):
    if not text:
        return ""
    if ext in (".html", ".htm"):
        m = re.search(r"(?is)<title>(.*?)</title>", text) or re.search(r"(?is)<h1[^>]*>(.*?)</h1>", text)
        return re.sub(r"<[^>]+>|\s+", " ", m.group(1)).strip()[:120] if m else ""
    m = re.search(r"(?m)^#{1,3}\s+(.+)$", text)
    if m:
        return m.group(1).strip()[:120]
    m = re.search(r"(?m)^\s*(?:--|#|//)\s*(\S.{8,})$", text)
    return m.group(1).strip()[:120] if m else ""


def csv_summary(text):
    rows = list(csv.reader(io.StringIO(text)))
    header = rows[0] if rows else []
    return header, max(0, len(rows) - 1)


def xlsx_summary(path):
    """First sheet's header row + row count, stdlib only."""
    try:
        with zipfile.ZipFile(path) as z:
            shared = []
            if "xl/sharedStrings.xml" in z.namelist():
                xml = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
                shared = [re.sub(r"<[^>]+>", "", s) for s in re.findall(r"(?s)<si>(.*?)</si>", xml)]
            sheets = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
            if not sheets:
                return [], 0
            xml = z.read(sheets[0]).decode("utf-8", "replace")
            rows = re.findall(r"(?s)<row[^>]*>(.*?)</row>", xml)
            header = []
            if rows:
                for c in re.findall(r'(?s)<c([^>]*)>(.*?)</c>', rows[0]):
                    v = re.search(r"<v>(.*?)</v>", c[1]) or re.search(r"<t[^>]*>(.*?)</t>", c[1])
                    val = v.group(1) if v else ""
                    if 't="s"' in c[0] and val.isdigit() and int(val) < len(shared):
                        val = shared[int(val)]
                    header.append(val)
            return header, max(0, len(rows) - 1)
    except (OSError, zipfile.BadZipFile, KeyError):
        return [], 0


def safe_file_name(name):
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name or "artifact").strip()
    return name[:120] or "artifact"


class Collector:
    """Files artifacts into tickets. All writes happen in the agent (single writer)."""

    def __init__(self, store, cfg, state):
        self.store, self.cfg, self.state = store, cfg, state
        acfg = cfg.get("artifacts") or {}
        self.text_max = int(acfg.get("text_max_bytes") or 2_000_000)
        self.bin_max = int(acfg.get("binary_max_bytes") or 25_000_000)
        self.push_bin = {("." + e.lower().lstrip(".")) for e in acfg.get("push_binary_types") or []}
        self.changed_tickets = set()
        self.unfiled = []

    # ---------------------------------------------------------------- recording
    def rec(self, t, key):
        tk = self.store.ticket(t)
        return tk.setdefault("artifacts", {}).get(key)

    def file(self, t, src, day, source, session="", content=None, purpose="", move=False, origin=None):
        """File one artifact (path on disk, or captured content when the file is gone). Returns record or None."""
        if not t:
            return None
        tk = self.store.ticket(t)
        arts = tk.setdefault("artifacts", {})
        key = common.stable_id(common.norm_path(origin or src))
        old = arts.get(key)
        exists = bool(src) and os.path.isfile(src)
        if exists:
            size = os.path.getsize(src)
            digest = file_sha(src)
        elif content is not None:
            data = content.encode("utf-8")
            size, digest = len(data), hashlib.sha1(data).hexdigest()
        else:
            return old
        if size < 50:
            return old
        if old and old.get("sha") == digest:
            if move and exists:
                self._remove(src)
            return old
        name = os.path.basename(origin or src)
        ext = os.path.splitext(name)[1].lower()
        date = (old or {}).get("date") or day
        stored_name = (old or {}).get("stored_name") or self._unique_name(arts, key, "%s_%s" % (date, safe_file_name(name)))
        adir = common.hp("tickets", t, "artifacts")
        ldir = os.path.join(adir, "local")
        rec = dict(old or {}, id=key, date=date, name=name, kind=kind_of(name) or "doc", source=(origin or src).replace("\\", "/"),
                   session=session or (old or {}).get("session", ""), sha=digest, size=size, last=day, source_kind=source,
                   stored_name=stored_name, versions=int((old or {}).get("versions") or 0) + 1)
        text = None
        if is_text(name) or ext == ".xlsx":
            if is_text(name):
                if exists and size > self.text_max:
                    rec.update(storage="metadata-only", note="larger than %d MB" % (self.text_max // 1_000_000))
                    return self._save(t, arts, key, rec)
                text = common.read_text(src) if exists else content
                if text is None:
                    return old
        if ext in (".csv", ".tsv"):
            header, n = csv_summary(text)
            self._copy_local(src, content, ldir, stored_name)
            body = "# %s\n\n_Export summary: data rows are never pushed; the original is in `local/`._\n\nRows: %d\n\nColumns: %s\n" % (
                name, n, ", ".join("`%s`" % h for h in header) or "—")
            common.write_text(os.path.join(adir, stored_name + ".summary.md"), redact(body))
            rec.update(storage="local-only + summary", summary=stored_name + ".summary.md", rows=n, local=stored_name)
        elif ext == ".xlsx":
            header, n = xlsx_summary(src) if exists else ([], 0)
            self._copy_local(src, None, ldir, stored_name, rotate=True)
            body = "# %s\n\n_Export summary: data rows are never pushed; the original is in `local/`._\n\nRows: %d\n\nColumns (first sheet): %s\n" % (
                name, n, ", ".join("`%s`" % h for h in header) or "—")
            common.write_text(os.path.join(adir, stored_name + ".summary.md"), redact(body))
            rec.update(storage="local-only + summary", summary=stored_name + ".summary.md", rows=n, local=stored_name)
        elif text is not None:
            pushed = text
            if ext in (".html", ".htm"):
                stripped = strip_data_uris(text)
                if stripped != text:
                    self._copy_local(src, content, ldir, stored_name)
                    rec["local"] = stored_name
                    rec["note"] = "embedded images removed from the pushed copy; original in local/"
                pushed = stripped
            red = redact(pushed)
            common.write_text(os.path.join(adir, stored_name), red)
            rec.update(storage="pushed", stored=stored_name)
            if not rec.get("purpose_set"):
                rec["purpose"] = purpose or rec.get("purpose") or title_of(text, ext)
        else:
            if not exists:
                return old
            if size > self.bin_max:
                rec.update(storage="metadata-only", note="larger than %d MB" % (self.bin_max // 1_000_000))
                return self._save(t, arts, key, rec)
            self._copy_local(src, None, ldir, stored_name, rotate=True)
            rec.update(storage="local-only", local=stored_name)
            if ext in self.push_bin:
                shutil.copy2(src, os.path.join(adir, stored_name))
                rec.update(storage="pushed", stored=stored_name)
        if not purpose:
            pp = (self.state.get("artifact_purposes") or {}).get(name.lower())
            if pp and pp[0] == t:
                purpose = pp[1]
        if purpose:
            rec["purpose"], rec["purpose_set"] = purpose, True
        if move and exists:
            self._remove(src)
            rec["moved_from"] = src.replace("\\", "/")
        return self._save(t, arts, key, rec)

    def link(self, t, url, title, day, session="", local_file=None):
        tk = self.store.ticket(t)
        arts = tk.setdefault("artifacts", {})
        key = common.stable_id(url)
        old = arts.get(key)
        rec = dict(old or {}, id=key, date=(old or {}).get("date") or day, name=title or url, kind="link", source=url,
                   session=session, storage="link", last=day, versions=(old or {}).get("versions") or 1)
        rec["purpose"] = rec.get("purpose") or title or ""
        if local_file and os.path.isfile(local_file):
            snap = self.file(t, local_file, day, "artifact-source", session, purpose=title)
            if snap:
                rec["snapshot"] = snap.get("stored") or snap.get("local")
        if old == rec:
            return old
        return self._save(t, arts, key, rec)

    def note_in_folder(self, t, path, day):
        """A file you put straight into tickets/<T>/: indexed where it is (no copy)."""
        tk = self.store.ticket(t)
        arts = tk.setdefault("artifacts", {})
        key = common.stable_id(common.norm_path(path))
        digest = file_sha(path)
        old = arts.get(key)
        if old and old.get("sha") == digest:
            return old
        name = os.path.basename(path)
        txt = common.read_text(path) if is_text(name) else ""
        rec = dict(old or {}, id=key, date=(old or {}).get("date") or day, name=name, kind=kind_of(name) or "doc", source="you",
                   storage="in ticket folder", stored="../" + name, sha=digest, size=os.path.getsize(path), last=day,
                   versions=int((old or {}).get("versions") or 0) + 1)
        rec["purpose"] = rec.get("purpose") or title_of(txt, os.path.splitext(name)[1].lower()) or txt.strip().split("\n")[0][:120]
        return self._save(t, arts, key, rec)

    def set_purposes(self, t, items):
        tk = self.store.ticket(t)
        arts = tk.setdefault("artifacts", {})
        for it in items or []:
            base = os.path.basename((it.get("path") or "").replace("\\", "/")).lower()
            for a in arts.values():
                if base and a.get("name", "").lower() == base and it.get("purpose") and not a.get("purpose_set"):
                    if a.get("purpose") != it["purpose"]:
                        a["purpose"], a["purpose_set"] = it["purpose"], True
                        self.changed_tickets.add(t)
                        self.store.touch_ticket(t)

    @staticmethod
    def _unique_name(arts, key, base):
        """Two different sources with the same file name never overwrite each other."""
        taken = {a.get("stored_name") for k, a in arts.items() if k != key}
        if base not in taken:
            return base
        stem, ext = os.path.splitext(base)
        n = 2
        while "%s-%d%s" % (stem, n, ext) in taken:
            n += 1
        return "%s-%d%s" % (stem, n, ext)

    def _save(self, t, arts, key, rec):
        arts[key] = rec
        self.changed_tickets.add(t)
        self.store.touch_ticket(t)
        return rec

    def _copy_local(self, src, content, ldir, stored_name, rotate=False):
        common.ensure_dir(ldir)
        dst = os.path.join(ldir, stored_name)
        if rotate and os.path.exists(dst):
            vdir = os.path.join(ldir, ".versions")
            common.ensure_dir(vdir)
            # keep the last 3 versions: current + .1 + .2
            p1, p2 = os.path.join(vdir, stored_name + ".1"), os.path.join(vdir, stored_name + ".2")
            if os.path.exists(p1):
                os.replace(p1, p2)
            shutil.copy2(dst, p1)
        if src and os.path.isfile(src):
            shutil.copy2(src, dst)
        elif content is not None:
            with open(dst, "w", encoding="utf-8", newline="") as f:
                f.write(content)

    @staticmethod
    def _remove(src):
        try:
            os.remove(src)
        except OSError:
            pass


# --------------------------------------------------------------------------- git helpers

def tracked_before(cfg, repo, path, since_iso):
    """True when the file was already committed before the session started (then it's a code change)."""
    if not repo:
        return False
    rc, so, _ = gitro.run(cfg, repo, ["log", "--diff-filter=A", "--format=%aI", "--", path], timeout=20)
    if rc != 0 or not so.strip():
        return False
    first = common.parse_ts(so.strip().splitlines()[-1])
    start = common.parse_ts(since_iso)
    return bool(first and start and first < start)


def untracked(cfg, repo):
    rc, so, _ = gitro.run(cfg, repo, ["ls-files", "--others", "--exclude-standard", "-z"], timeout=30)
    if rc != 0:
        return []
    return [os.path.join(repo, p) for p in so.split("\0") if p]


# --------------------------------------------------------------------------- index

def render_index(t, tk):
    arts = sorted((tk.get("artifacts") or {}).values(), key=lambda a: (a.get("date", ""), a.get("name", "")), reverse=True)
    L = ["# %s — Artifacts" % t, "",
         "_Everything made while investigating this ticket. Text copies are redacted; data exports and binaries stay in "
         "`local/` (git-ignored, this machine only)._", ""]
    if not arts:
        return "\n".join(L + ["_No artifacts yet._", ""])
    L += ["| Date | Artifact | Kind | Purpose | Source | Stored | Versions |", "|---|---|---|---|---|---|---|"]
    for a in arts:
        name = a.get("name", "")
        if a.get("kind") == "link":
            art = "[%s](%s)" % (cell(name), a["source"])
        elif a.get("stored"):
            art = "[%s](%s)" % (cell(name), a["stored"].replace(" ", "%20"))
        elif a.get("summary"):
            art = "%s ([summary](%s))" % (cell(name), a["summary"].replace(" ", "%20"))
        else:
            art = cell(name)
        stored = a.get("storage", "")
        if a.get("local") and "local" not in stored:
            stored += " + original in local/"
        if a.get("note"):
            stored += " — " + a["note"]
        src = a.get("source", "")
        if a.get("kind") != "link":
            src = "`%s`" % src.replace("`", "'") if src and src != "you" else (src or "")
        if a.get("session"):
            src += " (session %s)" % a["session"][:8]
        L.append("| %s | %s | %s | %s | %s | %s | %s |" % (a.get("date", ""), art, a.get("kind", ""), cell(a.get("purpose")) or "—",
                                                          cell(src), cell(stored), a.get("versions", 1)))
    return "\n".join(L) + "\n"


def cell(s):
    return re.sub(r"\s+", " ", str(s or "")).replace("|", "\\|").strip()


def latest(tk, n=5):
    return sorted((tk.get("artifacts") or {}).values(), key=lambda a: (a.get("last", ""), a.get("date", "")), reverse=True)[:n]
