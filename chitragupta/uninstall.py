"""Remove the worklog's footprint from Claude Code and Windows; keep all data.

Removes: our hooks + permission rule from ~/.claude/settings.json (backup first), the CLAUDE.md block,
the worklog skill, the WorklogAgent scheduled task (or Startup launcher).
Keeps: everything in the Worklog folder, including data/, daily/, tickets/ and the git repo.
"""
import datetime as _dt
import json
import os
import shutil
import sys

from . import common
from . import install


def main():
    out = []
    if os.path.exists(install.SETTINGS):
        stamp = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        backup = install.SETTINGS + ".worklog-uninstall-bak." + stamp
        shutil.copy2(install.SETTINGS, backup)
        s = install.strip_ours(install.read_settings())
        tmp = install.SETTINGS + ".worklog.tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(install.dump(s))
        json.load(open(tmp, encoding="utf-8"))
        os.replace(tmp, install.SETTINGS)
        out.append("hooks + permission removed from settings.json (backup: %s)" % backup)
    txt = common.read_text(install.CLAUDE_MD)
    i, j = txt.find(install.MD_START), txt.find(install.MD_END)
    if i >= 0 and j > i:
        new = (txt[:i].rstrip("\n") + "\n" + txt[j + len(install.MD_END):].lstrip("\n")).strip("\n")
        if new.strip():
            common.write_text(install.CLAUDE_MD, new + "\n")
        else:
            os.remove(install.CLAUDE_MD)
        out.append("CLAUDE.md block removed")
    skill_dir = os.path.dirname(install.SKILL)
    if os.path.isdir(skill_dir):
        shutil.rmtree(skill_dir)
        out.append("skill removed")
    rc, so, se = common.run(["schtasks", "/delete", "/tn", install.TASK_NAME, "/f"], timeout=60)
    out.append("scheduled task removed" if rc == 0 else "scheduled task: " + (se or so).strip())
    if os.path.exists(install.startup_launcher_path()):
        os.remove(install.startup_launcher_path())
        out.append("Startup launcher removed")
    out.append("Data kept in %s" % common.home())
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass  # pythonw has no console
    sys.exit(main())
