"""Path checking converges with fair budgets and stable commit caches."""
import unittest
from unittest import mock
from tests.support import Isolated,write_config

class PathBudget(Isolated,unittest.TestCase):
    def setUp(self):
        self.isolate();self.cfg={"ticket_prefixes":["ABC"]};write_config(self.home,self.cfg)

    def seed(self,st):
        from chitragupta import common
        branches={}
        for n in (1,2):
            t="ABC-100%d" % n;tk=st.ticket(t);tk["repos"]=["shop-api"]
            tk["files"]={"src/F%d.java" % i:{"path":"src/F%d.java" % i,"verified":None,"repo":"shop-api"} for i in range(4)}
            branches[t]={"ticket":t,"tip":str(n)*40};st.touch_ticket(t)
        common.write_json(common.hp("data","git","shop-api","branches.json"),branches)

    def test_low_budget_rotates_tickets(self):
        from chitragupta import pathcheck,store,gitro
        st=store.Store();self.seed(st)
        with mock.patch.object(pathcheck,"repo_root",return_value="synthetic"),mock.patch.object(gitro,"run",return_value=(0,"","")):
            for _ in range(2):self.assertEqual(pathcheck.run(st,self.cfg,budget=1),1)
        for t in ("ABC-1001","ABC-1002"):
            self.assertTrue(any(f["verified"] is True for f in st.ticket(t)["files"].values()))

    def test_committed_file_lists_are_settled_before_path_budget_is_spent(self):
        from chitragupta import pathcheck,store,gitro
        st=store.Store();self.seed(st);tk=st.ticket("ABC-1001")
        tk["files"]["src/Edit.java"]={"path":"src/Edit.java","repo":"shop-api","changed":True,"verified":True,"verified_by":"edit"}
        tk["commits"]={"a"*40:{"sha":"a"*40,"repo_name":"shop-api"}}
        def run(cfg,root,args,**kw):
            return (0,"\x1e"+"a"*40+"\nsrc/Edit.java\n","") if args[0]=="log" else (0,"","")
        with mock.patch.object(pathcheck,"repo_root",return_value="synthetic"),mock.patch.object(gitro,"run",side_effect=run):pathcheck.run(st,self.cfg,budget=2)
        self.assertIs(tk["files"]["src/Edit.java"].get("committed"),True)

    def test_unknown_repo_is_terminal_and_head_prefers_remote(self):
        from chitragupta import common,pathcheck,store
        st=store.Store();self.seed(st);rec=st.ticket("ABC-1001")["files"]["src/F0.java"]
        with mock.patch.object(pathcheck,"repo_root",return_value=None):
            pathcheck.run(st,self.cfg)
            self.assertFalse(rec["verified"]);self.assertEqual(rec["verified_by"],"unknown-repo")
            at=rec["checked_at"];pathcheck.run(st,self.cfg);self.assertEqual(rec["checked_at"],at)
        branches={"feature/ABC-1001":{"ticket":"ABC-1001","remote_tip":"r"*40,"tip":"l"*40}}
        common.write_json(common.hp("data","git","shop-api","branches.json"),branches)
        r=pathcheck._Run(store.Store(),self.cfg,40)
        self.assertEqual(r.heads("ABC-1001","shop-api","synthetic"),["r"*40])

    def test_missing_commit_does_not_poison_batch_or_repeat_forever(self):
        from chitragupta import pathcheck,store,gitro
        good,bad="a"*40,"b"*40;calls=[]
        def invoke(cfg,root,args,**kw):
            calls.append(args)
            return (128,"","bad object") if bad in args else (0,"\x1e"+good+"\nsrc/Kept.java\n","")
        r=pathcheck._Run(store.Store(),self.cfg,10)
        with mock.patch.object(pathcheck,"repo_root",return_value="synthetic"),mock.patch.object(gitro,"run",side_effect=invoke):
            got=r.files_of("shop-api",[good,bad]);self.assertEqual(got.get(good),["src/Kept.java"])
            before=len(calls);self.assertEqual(r.files_of("shop-api",[good,bad]).get(good),["src/Kept.java"])
            self.assertEqual(len(calls),before);self.assertIn("shop-api|"+bad,r.commit_files)

    def test_many_pending_paths_share_one_commit_tree_read(self):
        from chitragupta import common,pathcheck,store,gitro
        st=store.Store();tk=st.ticket("ABC-1001");tk["repos"]=["shop-api"]
        paths=["src/F%d.java" % n for n in range(30)]
        tk["files"]={p:{"path":p,"repo":"shop-api","verified":None} for p in paths};st.touch_ticket("ABC-1001")
        common.write_json(common.hp("data","git","shop-api","branches.json"),{"feature/a":{"ticket":"ABC-1001","remote_tip":"a"*40}})
        with mock.patch.object(pathcheck,"repo_root",return_value="synthetic"),mock.patch.object(gitro,"run",return_value=(0,"\0".join(paths)+"\0","")) as run:
            self.assertEqual(pathcheck.run(st,self.cfg,budget=2),1)
            self.assertEqual(run.call_args.args[2][0],"ls-tree")
        self.assertTrue(all(f["verified"] for f in tk["files"].values()))
        self.assertTrue(gitro.check(["ls-tree","-r","-z","--name-only","a"*40]))
        with self.assertRaises(gitro.NotAllowed):gitro.check(["ls-tree","--output=unwanted","a"*40])
