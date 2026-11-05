"""An empty own-commit list is a cached fact, invalidated when local, remote or parent tips move."""
import unittest
from types import SimpleNamespace
from unittest import mock
from tests.support import Isolated,write_config

class EmptyBranchFacts(Isolated,unittest.TestCase):
    def setUp(self):self.isolate();write_config(self.home,{"ticket_prefixes":["ABC"]})

    def test_empty_fact_is_reused_and_remote_tip_invalidates(self):
        from chitragupta import common,gittrack,store
        b="feature/ABC-1001";info={"sha":"a"*40,"date":"2026-09-01","subject":"Synthetic branch","upstream":""}
        r=SimpleNamespace(refs={b:dict(info,kind="local"),"origin/"+b:dict(info,kind="remote")},remote="origin",name="shop-api",
          data=SimpleNamespace(branches={}),role=lambda name:"qa",sha=lambda ref:"b"*40)
        tr=gittrack.Tracker(common.load_config(),{},store.Store());tr.link=lambda *args:("ABC-1001","branch")
        old={"v":gittrack.ALGO,"parent":"qa","parent_ref":"origin/qa","own":[]}
        with mock.patch.object(tr,"commit_facts") as facts:
            old=tr.branch_record(r,b,old,[],{},["ABC"],{},set())
            self.assertEqual(facts.call_count,1)
            old=tr.branch_record(r,b,old,[],{},["ABC"],{},set())
            self.assertEqual(facts.call_count,1)
            r.refs["origin/"+b]["sha"]="c"*40
            tr.branch_record(r,b,old,[],{},["ABC"],{},set())
            self.assertEqual(facts.call_count,2)
