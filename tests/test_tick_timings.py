"""Tick timing uses a monotonic clock, retains failed steps, and warns about slow runs."""
import unittest
from unittest import mock
from types import SimpleNamespace
from tests.support import Isolated,write_config

class TickTimings(Isolated,unittest.TestCase):
    def setUp(self):self.isolate();write_config(self.home,{"ticket_prefixes":["ABC"]})

    def test_step_duration_survives_an_exception(self):
        from chitragupta import agent
        a=agent.Agent.__new__(agent.Agent)
        with mock.patch.object(agent.time,"monotonic",side_effect=[100,101.25]):
            with self.assertRaisesRegex(ValueError,"failed"):
                a._timed("pathcheck",lambda:(_ for _ in ()).throw(ValueError("failed")))
        self.assertEqual(a.steps,{"pathcheck":1250})

    def test_cmd_records_monotonic_total_and_steps(self):
        from chitragupta import agent,migrate,ticklog
        a=SimpleNamespace(health={},steps={"pathcheck":45},tick=lambda:None)
        lock=SimpleNamespace(acquire=lambda:True,release=lambda:None)
        with mock.patch.object(agent,"Agent",return_value=a),mock.patch.object(agent,"Lock",return_value=lock),mock.patch.object(agent,"save_state"),mock.patch.object(migrate,"run_in_tick"),mock.patch.object(agent.time,"monotonic",side_effect=[10,10.5]),mock.patch.object(agent.time,"time",side_effect=[100,-999]):
            self.assertEqual(agent.cmd_tick(),0)
        row=ticklog.recent()[-1];self.assertEqual(row["ms"],500);self.assertEqual(row["steps"],a.steps)

    def test_health_warns_above_sixty_seconds_until_a_faster_run(self):
        from chitragupta import ticklog
        from chitragupta.dashboard import api
        ticklog.record("ok",61001,steps={"sources":55000,"pathcheck":4000})
        ticklog.record("skipped-busy")
        warning=ticklog.slow_warning();self.assertIn("61.0s",warning);self.assertIn("sources",warning)
        self.assertIn(warning,api.status()["warnings"])
        ticklog.record("ok",60000,steps={"sources":2000})
        self.assertEqual(ticklog.slow_warning(),"")
