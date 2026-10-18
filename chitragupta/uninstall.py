"""Remove Chitragupta's footprint outside the data home; keep all data.

    python -m chitragupta uninstall [--json]

Removes: the `Chitragupta` scheduled task (or its Startup-folder launcher), the stable runtime
(<user dir>/runtime), the data home's git pre-commit hook if it calls that runtime, the recall skill (only if its
frontmatter name is ours), the CLAUDE.md block between
<!-- chitragupta:start --> and <!-- chitragupta:end --> (backup first), and the home pointer.
Keeps: everything in the data home (daily/, tickets/, data/, the git repo, config). The plugin itself is removed
with `/plugin uninstall cg`.
"""
import json
import os
import sys

from . import common
from . import install
from . import plugin
from . import setup
from .safety import paths


def run():
    out = []
    home = common.home()
    from . import scheduler
    out += scheduler.unregister()
    out += install.remove_shortcuts()

    rt = plugin.runtime_dir()
    if os.path.isdir(rt):
        paths.rmtree(rt)
        out.append("runtime removed: %s" % rt if not os.path.isdir(rt) else "runtime partly removed (files in use): %s" % rt)

    sk = setup.skill_installed()
    if sk["ours"]:
        paths.rmtree(os.path.dirname(sk["path"]))
        out.append("recall skill removed: %s" % os.path.dirname(sk["path"]))
    elif os.path.exists(sk["path"]):
        out.append("recall skill left alone: %s belongs to another skill (name: %s)" % (sk["path"], sk.get("name")))

    md = plugin.claude_md_path()
    txt = common.read_text(md)
    if setup.MD_START in txt or setup.MD_END in txt:
        problem = setup.check_markers(txt)
        if problem:
            out.append("CLAUDE.md block left alone: %s" % problem)
        else:
            bak = md + ".chitragupta.bak"
            paths.copy(md, bak)
            new = setup.remove_block(txt)
            if new.strip():
                common.write_text(md, new)
            else:
                paths.delete(md)
            out.append("CLAUDE.md block removed (backup %s)" % bak)

    # The data home's git pre-commit hook calls the runtime removed above; drop it if it's ours so manual
    # commits keep working (another worklog tool rewrites its own hook on its next run).
    hook = os.path.join(home, ".git", "hooks", "pre-commit")
    launcher = common.launcher().replace("\\", "/")
    if os.path.isfile(hook) and launcher.lower() in common.read_text(hook).replace("\\", "/").lower():
        paths.delete(hook)
        out.append("data home pre-commit hook removed: %s" % hook)

    for p in (common.home_pointer(), os.path.join(common.user_dir(), "maintenance.lock")):
        if os.path.exists(p):
            paths.delete(p)
            if p == common.home_pointer():
                out.append("home pointer removed")
    try:
        paths.rmdir(common.user_dir())  # only when empty
    except OSError:
        pass
    out.append("Data kept in %s" % home)
    out.append("To remove the plugin itself: /plugin uninstall cg")
    return out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    out = run()
    print(json.dumps(out, indent=1) if "--json" in argv else "\n".join(out))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass  # pythonw has no console
    sys.exit(main())
