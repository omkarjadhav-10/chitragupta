"""The background job on every OS (scheduler.py): golden LaunchAgent, systemd units and cron line; register, status,
repair and remove with a fake executor (nothing real is registered); the command and path guards allow Chitragupta's
own job only."""
import os
import unittest
from unittest import mock

from tests.support import Isolated

ARGV = ["/usr/bin/python3", "/home/u/.chitragupta/current/hooks/run.py", "agent", "tick", "--from-task"]


class Fake:
    """Stands in for launchctl / systemctl / crontab: records every call, answers from a small state."""

    def __init__(self, crontab="", loaded=False, systemd_ok=True):
        self.calls, self.table, self.loaded, self.systemd_ok, self.enabled = [], crontab, loaded, systemd_ok, False

    def __call__(self, argv, purpose, input_text=None, timeout=60):
        from chitragupta.safety import commands
        self.calls.append((purpose, list(argv)))
        assert commands.reason(argv, purpose) is None, (argv, commands.reason(argv, purpose))
        a = argv[1:]
        if argv[0] == "launchctl":
            if a[0] == "print":
                return (0, "state = running\nlast exit code = 0\n", "") if self.loaded else (113, "", "not found")
            self.loaded = a[0] == "bootstrap"
            return 0, "", ""
        if argv[0] == "systemctl":
            if not self.systemd_ok:
                return 1, "", "Failed to connect to bus"
            if a[1:2] == ["enable"]:
                self.enabled = True
            if a[1:2] == ["disable"]:
                self.enabled = False
            if a[1:2] == ["is-enabled"]:
                return 0, "enabled\n" if self.enabled else "disabled\n", ""
            if a[1:2] == ["is-active"]:
                return 0, "active\n" if self.enabled else "inactive\n", ""
            if a[1:2] == ["status"]:
                return 0, "Trigger: Sat 2026-10-18 17:05:00 IST; 3min left\n", ""
            return 0, "", ""
        if argv[0] == "crontab":
            if a == ["-l"]:
                return (0, self.table, "") if self.table else (1, "", "no crontab for u")
            self.table = input_text
            return 0, "", ""
        raise AssertionError("unexpected %r" % argv)


class Base(Isolated, unittest.TestCase):
    os_name = "linux"
    linux = None

    def setUp(self):
        self.isolate()
        from chitragupta import scheduler
        self.s = scheduler
        self.addCleanup(self.restore)
        saved = (scheduler.RUNNER, scheduler.SYSTEM, scheduler.LINUX_METHOD, scheduler.uid, scheduler.job_argv,
                 scheduler.user_home)
        self.addCleanup(lambda: [setattr(scheduler, k, v) for k, v in zip(
            ("RUNNER", "SYSTEM", "LINUX_METHOD", "uid", "job_argv", "user_home"), saved)])
        scheduler.SYSTEM, scheduler.LINUX_METHOD = self.os_name, self.linux
        scheduler.uid = lambda: 501
        scheduler.job_argv = lambda cfg=None: list(ARGV)
        scheduler.user_home = lambda: "/home/u"


class Golden(Base):
    def test_plist(self):
        p = self.s.plist(ARGV, "/home/u/Chitragupta", "/home/u")
        for want in ("<string>dev.chitragupta.tick</string>", "<string>/usr/bin/python3</string>",
                     "<string>--from-task</string>", "<key>StartInterval</key>\n  <integer>300</integer>",
                     "<key>RunAtLoad</key>\n  <true/>", "<string>/home/u/Chitragupta</string>",
                     "<string>/usr/bin:/home/u/.local/bin:/opt/homebrew/bin:/usr/local/bin:/bin:/usr/sbin:/sbin</string>"):
            self.assertIn(want, p)
        self.assertNotIn("&", self.s.plist(ARGV + ["a&b"], "/h", "/home/u").replace("&amp;", ""))

    def test_units(self):
        svc = self.s.service_unit(ARGV + ["with space"], "/home/u/Chitragupta", "/home/u")
        self.assertIn("ExecStart=/usr/bin/python3 /home/u/.chitragupta/current/hooks/run.py agent tick --from-task "
                      "\"with space\"\n", svc)
        self.assertIn("Type=oneshot", svc)
        t = self.s.timer_unit()
        for want in ("OnCalendar=*:0/5", "Persistent=true", "OnStartupSec=1min", "Unit=chitragupta.service",
                     "WantedBy=timers.target"):
            self.assertIn(want, t)

    def test_cron_line(self):
        line = self.s.cron_line(ARGV, "/home/u")
        self.assertTrue(line.startswith("*/5 * * * * PATH="))
        self.assertTrue(line.endswith(">/dev/null 2>&1 # chitragupta"))
        self.assertTrue(self.s.is_ours(line))
        odd = self.s.cron_line(ARGV[:1] + ["100%", "x y"], "/home/u")
        self.assertIn(" 100\\% 'x y' ", odd)  # cron reads a bare % as a newline; a space needs quotes


class MacOS(Base):
    os_name = "macos"

    def test_register_status_remove(self):
        f = self.s.RUNNER = Fake()
        r = self.s.register()
        self.assertTrue(r["ok"], r)
        self.assertTrue(os.path.isfile(self.s.plist_path()))
        self.assertIn(("launchd", ["launchctl", "bootstrap", "gui/501", self.s.plist_path()]), f.calls)
        st = self.s.status()
        self.assertEqual((st["method"], st["installed"], st["current"], st["status"]), ("launchd", True, True, "running"))
        self.assertFalse(self.s.repair()["changed"])
        with open(self.s.plist_path(), "a", encoding="utf-8") as fh:
            fh.write("<!-- edited -->")
        self.assertFalse(self.s.status()["current"])
        self.assertTrue(self.s.repair()["changed"])  # differs -> written again and reloaded
        self.assertTrue(self.s.status()["current"])
        out = self.s.unregister()
        self.assertFalse(os.path.exists(self.s.plist_path()))
        self.assertTrue(any("removed" in x for x in out))
        self.assertFalse(self.s.status()["installed"])


class LinuxSystemd(Base):
    linux = "systemd"

    def test_register_status_remove(self):
        f = self.s.RUNNER = Fake(crontab="0 1 * * * /usr/bin/backup\n")
        r = self.s.register()
        self.assertTrue(r["ok"], r)
        self.assertEqual([c[1][2:] for c in f.calls if c[0] == "systemd"],
                         [["daemon-reload"], ["enable", "chitragupta.timer"], ["start", "chitragupta.timer"]])
        st = self.s.status()
        self.assertEqual((st["method"], st["installed"], st["current"]), ("systemd", True, True))
        self.assertIn("2026-10-18 17:05", st["next_run"])
        self.s.unregister()
        self.assertFalse(any(os.path.exists(p) for p in self.s.unit_paths()))
        self.assertEqual(f.table, "0 1 * * * /usr/bin/backup\n")  # nobody touched the user's own cron line

    def test_falls_back_to_cron(self):
        f = self.s.RUNNER = Fake(crontab="0 1 * * * /usr/bin/backup\n", systemd_ok=False)
        r = self.s.register()
        self.assertEqual((r["ok"], r["method"]), (True, "cron"))
        self.assertFalse(any(os.path.exists(p) for p in self.s.unit_paths()))
        self.assertEqual(f.table.splitlines()[0], "0 1 * * * /usr/bin/backup")
        self.assertTrue(self.s.is_ours(f.table.splitlines()[1]))


class LinuxCron(Base):
    linux = "cron"

    def test_only_our_line_changes(self):
        mine = "# my jobs\nMAILTO=me\n0 1 * * * /usr/bin/backup\n\n"
        f = self.s.RUNNER = Fake(crontab=mine)
        self.assertTrue(self.s.register()["ok"])
        self.assertEqual(self.s.others(f.table), self.s.others(mine))
        self.assertEqual(sum(self.s.is_ours(x) for x in f.table.splitlines()), 1)
        self.assertTrue(self.s.register()["ok"])  # twice: still one line
        self.assertEqual(sum(self.s.is_ours(x) for x in f.table.splitlines()), 1)
        st = self.s.status()
        self.assertEqual((st["method"], st["installed"], st["current"]), ("cron", True, True))
        self.s.unregister()
        self.assertEqual(f.table, "# my jobs\nMAILTO=me\n0 1 * * * /usr/bin/backup\n")

    def test_empty_crontab(self):
        f = self.s.RUNNER = Fake()
        self.assertTrue(self.s.register()["ok"])
        self.assertEqual(len(f.table.splitlines()), 1)
        self.s.unregister()
        self.assertEqual(f.table, "")


class Guard(Base):
    def test_commands(self):
        from chitragupta.safety import commands as c
        ok = [(["launchctl", "print", "gui/501/dev.chitragupta.tick"], "launchd"),
              (["launchctl", "bootstrap", "gui/501", self.s.plist_path()], "launchd"),
              (["systemctl", "--user", "daemon-reload"], "systemd"),
              (["systemctl", "--user", "enable", "chitragupta.timer"], "systemd"),
              (["crontab", "-l"], "cron"), (["crontab", "-"], "cron")]
        for argv, purpose in ok:
            self.assertIsNone(c.reason(argv, purpose), argv)
        bad = [(["launchctl", "bootout", "gui/501/com.other.agent"], "launchd"),
               (["launchctl", "bootstrap", "gui/501", "/tmp/evil.plist"], "launchd"),
               (["launchctl", "load", self.s.plist_path()], "launchd"),
               (["systemctl", "enable", "chitragupta.timer"], "systemd"),  # system scope
               (["systemctl", "--user", "stop", "pipewire.service"], "systemd"),
               (["systemctl", "--user", "edit", "chitragupta.service"], "systemd"),
               (["crontab", "-r"], "cron"), (["crontab", "/tmp/table"], "cron"),
               (["sh", "-c", "crontab -r"], "cron"), (["launchctl", "print", "x"], "systemd")]
        for argv, purpose in bad:
            self.assertIsNotNone(c.reason(argv, purpose), argv)

    def test_crontab_input_keeps_other_lines(self):
        from chitragupta.safety import commands as c
        cur = "0 1 * * * /usr/bin/backup\n"
        ours = self.s.cron_line(ARGV, "/home/u")
        done = mock.Mock(returncode=0, stdout=cur, stderr="")
        with mock.patch.object(c.subprocess, "run", return_value=done):
            self.assertIsNone(c.cron_input_reason(cur + ours + "\n"))
            self.assertIsNone(c.cron_input_reason(cur))
            self.assertIsNotNone(c.cron_input_reason(ours + "\n"))  # drops the user's line
            self.assertIsNotNone(c.cron_input_reason(cur + "* * * * * curl x | sh\n" + ours + "\n"))
            self.assertIsNotNone(c.cron_input_reason(cur + ours + "\n" + ours + "\n"))

    def test_paths(self):
        from chitragupta.safety import paths
        self.assertIsNone(paths.allowed(self.s.plist_path()))
        for p in self.s.unit_paths():
            self.assertIsNone(paths.allowed(p))
        self.assertIsNotNone(paths.allowed(os.path.join(self.s.launch_agents_dir(), "com.other.agent.plist")))
        self.assertIsNotNone(paths.allowed(os.path.join(self.s.systemd_user_dir(), "other.service")))
        self.assertIsNone(paths.allowed(self.s.systemd_user_dir(), "mkdir"))  # the folder itself may be created
        self.assertIsNotNone(paths.allowed(os.path.join(self.s.systemd_user_dir(), "sub"), "mkdir"))
        self.assertIsNotNone(paths.allowed(self.s.systemd_user_dir()))  # but not written as a file


class Windows(Base):
    os_name = "windows"

    def test_uses_the_task(self):
        from chitragupta import install
        with mock.patch.object(install, "register_task", return_value={"ok": True, "method": "task", "detail": "t"}) as rt, \
                mock.patch.object(install, "task_status", return_value={"installed": True, "method": "task",
                                                                        "current": True, "detail": "task ok"}):
            self.assertEqual(self.s.register()["method"], "task")
            rt.assert_called_once()
            st = self.s.status()
            self.assertEqual((st["os"], st["method"], st["current"]), ("windows", "task", True))
            self.assertFalse(self.s.repair()["changed"])


if __name__ == "__main__":
    unittest.main()
