import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from ninecolab.accounts import AccountStore
from ninecolab.ui import DashboardService, make_handler


class TestDashboardServerGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store = AccountStore(Path(self.tmp.name) / ".9colab")
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(DashboardService(store))
        )
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _request(self, method, path, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status

    def test_foreign_host_header_is_rejected(self):
        self.assertEqual(
            self._request("GET", "/api/overview", {"Host": "evil.example"}), 403
        )

    def test_cross_site_form_post_is_rejected_and_does_not_change_mode(self):
        status = self._request(
            "POST",
            "/api/mode",
            {"Content-Type": "text/plain"},
            json.dumps({"mode": "C"}),
        )
        self.assertEqual(status, 403)

    def test_json_post_from_dashboard_is_accepted(self):
        status = self._request(
            "POST",
            "/api/mode",
            {"Content-Type": "application/json"},
            json.dumps({"mode": "C"}),
        )
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
