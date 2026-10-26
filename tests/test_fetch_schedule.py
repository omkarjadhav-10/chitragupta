"""Background fetch attempts rotate across due repositories, including failures."""
import os,unittest
from types import SimpleNamespace
from unittest import mock
from tests.support import Isolated,write_config

class FetchSchedule(Isolated,unittest.TestCase):
    def setUp(self):
        self.isolate();self.repos=[os.path.join(self.home,'shop%d' % n) for n in range(6)]
        for p in self.repos:os.makedirs(os.path.join(p,'.git'))
        self.cfg={"ticket_prefixes":["ABC"],"watched_repos":self.repos}
        write_config(self.home,self.cfg)

    def test_two_due_attempts_per_tick_and_failure_does_not_starve_later_repos(self):
        from chitragupta import gittrack,gitro,store
        state={};attempts=[]
        data={p:SimpleNamespace(meta={},branches={"feature/ABC-1001":{"ticket":"ABC-1001"}},touch=lambda name:None) for p in self.repos}
        def invoke(cfg,path,args,**kw):attempts.append(path);return 1,"","offline"
        with mock.patch.object(gitro,"locked",return_value=False),mock.patch.object(gitro,"run",side_effect=invoke):
            for n in range(3):
                tr=gittrack.Tracker(self.cfg,state,store.Store());tr.active_tickets=lambda:{"ABC-1001"}
                tr.refresh_repo=lambda path,**kw:tr.maybe_fetch(path,data[path])
                before=len(attempts);tr.refresh()
                self.assertEqual(len(attempts)-before,2)
        self.assertEqual(attempts,self.repos)

    def test_explicit_check_can_fetch_all_selected_repositories(self):
        from chitragupta import gittrack,gitro,store
        data={p:SimpleNamespace(meta={},branches={},touch=lambda name:None) for p in self.repos};attempts=[]
        tr=gittrack.Tracker(self.cfg,{},store.Store(),force_fetch=True);tr.active_tickets=lambda:{"ABC-1001"}
        tr.refresh_repo=lambda path,**kw:tr.maybe_fetch(path,data[path])
        with mock.patch.object(gitro,"locked",return_value=False),mock.patch.object(gitro,"run",side_effect=lambda cfg,path,args,**kw:(attempts.append(path) or (1,"","offline"))):tr.refresh()
        self.assertEqual(attempts,self.repos)
