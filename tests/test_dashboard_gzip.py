"""Dashboard JSON compression preserves content, HTTP framing, and explicit identity requests."""
import gzip,json,unittest
from tests import test_dashboard as fixture

class Gzip(fixture.Base):
    def setUp(self):
        super().setUp()
        self.srv,self.thread=fixture.server_mod().start_in_thread(0);self.port=self.srv.port

    def tearDown(self):
        self.srv.shutdown();self.srv.server_close();super().tearDown()

    get=fixture.TestHttp.get

    def test_roundtrip_and_head(self):
        path="/api/tickets/ABC-1001"
        plain,body=self.get(path)
        packed,data=self.get(path,headers={"Accept-Encoding":"gzip"})
        self.assertEqual(packed.getheader("Content-Encoding"),"gzip")
        self.assertEqual(json.loads(gzip.decompress(data)),json.loads(body))
        self.assertLess(len(data),len(body));self.assertEqual(int(packed.getheader("Content-Length")),len(data))
        self.assertIn("Accept-Encoding",packed.getheader("Vary"))
        head,empty=self.get(path,method="HEAD",headers={"Accept-Encoding":"gzip"})
        self.assertEqual(empty,b"");self.assertEqual(head.getheader("Content-Encoding"),"gzip")
        self.assertEqual(head.getheader("Content-Length"),packed.getheader("Content-Length"))

    def test_identity_and_q_zero(self):
        for header in ("identity","gzip;q=0, *;q=1","gzipish","gzip;q=invalid"):
            response,body=self.get("/api/tickets/ABC-1001",headers={"Accept-Encoding":header})
            self.assertIsNone(response.getheader("Content-Encoding"));self.assertIn("key",json.loads(body))
