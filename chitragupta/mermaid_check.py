"""Structural syntax check for the Mermaid we generate (flowchart, sequenceDiagram, erDiagram).
No Node/mmdc needed (Chitragupta runs no external renderer; `use_mmdc` is accepted and ignored).

    mermaid_check.py FILE.md...   exit 1 and list problems for every ```mermaid block
"""
import re
import sys

ID = r"[A-Za-z][A-Za-z0-9_]*"
QLABEL = r'"[^"\n]*"'
NODE = r"%s(?:\[%s\]|\[\(%s\)\]|\(\[%s\]\)|\(\(%s\)\)|\{%s\}|\(%s\)|\[\[%s\]\]|>%s\])?" % ((ID,) + (QLABEL,) * 8)
EDGE = r"(?:-->|---|-\.->|==>|--o|--x)"
FLOW_LINE = re.compile(r"^(?:%s(?:\s*%s(?:\|%s\|)?\s*%s)*|classDef\s+\w+\s+.+|class\s+[\w,]+\s+\w+|subgraph\s+%s(?:\[%s\])?|end|direction\s+(?:LR|RL|TB|TD|BT)|style\s+%s\s+.+)$"
                       % (NODE, EDGE, QLABEL, NODE, ID, QLABEL, ID))
SEQ_PART = re.compile(r"^(participant|actor)\s+(%s)(?:\s+as\s+([^;#\n]+))?$" % ID)
SEQ_MSG = re.compile(r"^(%s)\s*(->>|-->>|->|-->|-x|--x|-\)|--\))\s*([+-]?)(%s)\s*:\s*([^;\n]*)$" % (ID, ID))
SEQ_NOTE = re.compile(r"^Note\s+(?:over|left of|right of)\s+(%s)(?:\s*,\s*(%s))?\s*:\s*[^;\n]*$" % (ID, ID))
SEQ_BLOCK = re.compile(r"^(alt|else|opt|loop|par|and|critical|break|rect)\b[^;\n]*$|^end$|^autonumber$")
ER_CARD = r"(?:\|\||\|o|o\||\}o|o\{|\}\||\|\{)"
ER_REL = re.compile(r"^(%s)\s+%s(?:--|\.\.)%s\s+(%s)\s*:\s*(?:%s|\w+)$" % (r"[A-Za-z][\w-]*", ER_CARD, ER_CARD, r"[A-Za-z][\w-]*", QLABEL))
ER_OPEN = re.compile(r"^([A-Za-z][\w-]*)\s*\{$")
ER_ATTR = re.compile(r"^[A-Za-z][\w()\[\],-]*\s+[A-Za-z_][\w-]*(?:\s+(?:PK|FK|UK)(?:\s*,\s*(?:PK|FK|UK))*)?(?:\s+%s)?$" % QLABEL)


def blocks(md):
    return [m.group(1) for m in re.finditer(r"```mermaid\n(.*?)\n```", md or "", re.S)]


def check_block(src):
    lines = [l.strip() for l in src.split("\n")]
    lines = [l for l in lines if l and not l.startswith("%%")]
    if not lines:
        return ["empty diagram"]
    head, body, errs = lines[0], lines[1:], []
    if re.match(r"^(flowchart|graph)\s+(LR|RL|TB|TD|BT)$", head):
        depth = 0
        for l in body:
            if l.startswith("subgraph"):
                depth += 1
            elif l == "end":
                depth -= 1
            if not FLOW_LINE.match(l):
                errs.append("flowchart: bad line: " + l)
        if depth:
            errs.append("flowchart: unbalanced subgraph/end")
    elif head == "sequenceDiagram":
        declared, depth = set(), 0
        for l in body:
            m = SEQ_PART.match(l)
            if m:
                declared.add(m.group(2))
                continue
            m = SEQ_MSG.match(l)
            if m:
                for p in (m.group(1), m.group(4)):
                    if p not in declared:
                        errs.append("sequence: undeclared participant %s" % p)
                continue
            if SEQ_NOTE.match(l):
                continue
            if SEQ_BLOCK.match(l):
                depth += 1 if not l.startswith(("else", "and", "end", "autonumber")) else (-1 if l == "end" else 0)
                continue
            errs.append("sequence: bad line: " + l)
        if depth:
            errs.append("sequence: unbalanced block/end")
    elif head == "erDiagram":
        inside = False
        for l in body:
            if inside:
                if l == "}":
                    inside = False
                elif not ER_ATTR.match(l):
                    errs.append("erDiagram: bad attribute: " + l)
                continue
            if ER_OPEN.match(l):
                inside = True
            elif not ER_REL.match(l) and not re.match(r"^[A-Za-z][\w-]*$", l):
                errs.append("erDiagram: bad line: " + l)
        if inside:
            errs.append("erDiagram: unclosed entity")
    else:
        errs.append("unknown diagram type: " + head)
    return errs


def check(md, use_mmdc=False):
    errs = []
    for i, b in enumerate(blocks(md)):
        errs += ["block %d: %s" % (i + 1, e) for e in check_block(b)]
    return errs


if __name__ == "__main__":
    bad = 0
    for path in sys.argv[1:]:
        with open(path, encoding="utf-8") as f:
            for e in check(f.read(), use_mmdc=True):
                print("%s: %s" % (path, e))
                bad += 1
    sys.exit(1 if bad else 0)
