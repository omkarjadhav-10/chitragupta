"""One read-only host list per due repository; branch filtering stays local."""
import json,unittest
from types import SimpleNamespace
from unittest import mock
from tests.support import Isolated,write_config


class RepositoryPRs(Isolated,unittest.TestCase):
    def setUp(self):
        self.isolate();write_config(self.home,{"ticket_prefixes":["ABC"]})

    def test_due_branches_share_one_repository_list(self):
        from chitragupta import common,gittrack,hosts,store
        now=common.now(); data=SimpleNamespace(meta={"host":"azure","org":"acme","project":"shop","repo":"shop-api"},
          branches={b:{"on_remote":True,"remote_tip":b,"ticket":"ABC-1001"} for b in ("feature/a","feature/b")},prs={},touch=lambda x:None)
        r=SimpleNamespace(data=data,conf={},all_base_refs=lambda:{},path="synthetic",sha=lambda ref:"")
        tr=gittrack.Tracker(common.load_config(),{},store.Store(),now=now);tr.active_tickets=lambda:{"ABC-1001"}
        tr._merge_pr=lambda repo,prs,p:prs.update({str(p["number"]):p})
        result=[{"number":n,"source":b,"state":"open"} for n,b in enumerate(("feature/a","feature/b","unrelated"),1)]
        with mock.patch.object(hosts,"source_for",return_value="az"),mock.patch.object(hosts,"prs_for_repo",create=True,return_value=result) as batch, mock.patch.object(hosts,"prs_for_branch",return_value=[]) as branch:
            tr.refresh_prs(r)
            self.assertEqual(batch.call_count,1);self.assertEqual(branch.call_count,0)
            self.assertEqual(set(data.prs),{"1","2"});self.assertEqual(set(data.meta["pr_checked"]),set(data.branches))
            tr.refresh_prs(r);self.assertEqual(batch.call_count,1)
            data.branches["feature/a"]["remote_tip"]="newsha"
            tr.refresh_prs(r);self.assertEqual(batch.call_count,1)  # open PR waits for refresh interval

    def test_repo_cli_omits_branch_filter_and_preserves_states(self):
        from chitragupta import hosts
        fixtures={"az":({"org":"acme","project":"shop","repo":"shop-api"},[{"pullRequestId":1,"sourceRefName":"refs/heads/feature/a","status":"completed"}]),
          "gh":({"path":"acme/shop-api"},[{"number":1,"headRefName":"feature/a","state":"MERGED"}]),
          "glab":({"path":"acme/shop-api"},[{"iid":1,"source_branch":"feature/a","state":"merged"}])}
        for source,(info,rows) in fixtures.items():
            calls=[]
            def run(argv,**kw): calls.append(argv);return 0,json.dumps(rows),""
            with mock.patch.object(hosts,"cli_path",side_effect=lambda cfg,name:name):
                got=hosts.prs_for_repo({},info,source,run=run)
            self.assertEqual(len(calls),1);self.assertEqual(got[0]["state"],"merged")
            self.assertNotIn("--source-branch",calls[0]);self.assertNotIn("--head",calls[0])
