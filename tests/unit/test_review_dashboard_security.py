"""Regression tests for the Mission Control dashboard security review.

Covers: the undefined `load_config` NameError, DNS-rebinding Host checks,
bounded request bodies, static-file containment, handoff record traversal,
the kiro auto-import auth gate, exception chaining in the wizard, inline-script
escaping for OAuth callback pages.

Nothing here performs a real OAuth login, agent execution or credential write.
"""
from __future__ import annotations

import ast
import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ui.dashboard import dashboard
from ui.dashboard.dashboard import (
    MAX_REQUEST_BODY_BYTES,
    ThreadedHTTPServer,
    _is_trusted_local_file,
    _js_literal,
    is_allowed_host_header,
    is_public_path,
)

DASHBOARD_PY = Path(dashboard.__file__)


class _ServerMixin:
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadedHTTPServer(("127.0.0.1", 0), dashboard.MissionControlHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.token = dashboard.get_or_create_auth_token()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _raw(self, method, path, headers=None, body=None, host=None):
        """Send a request with full control of Host / Content-Length."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", host or f"127.0.0.1:{self.port}")
            for k, v in (headers or {}).items():
                conn.putheader(k, v)
            conn.endheaders()
            if body:
                conn.send(body)
            resp = conn.getresponse()
            data = resp.read().decode("utf-8", errors="replace")
            return resp.status, data
        finally:
            conn.close()

    def _auth(self):
        return {"Authorization": f"Bearer {self.token}"}


class TestLoadConfigNameError(unittest.TestCase):
    """dashboard.py called load_config() without importing it (ruff F821)."""

    def test_load_config_is_bound(self):
        self.assertTrue(callable(getattr(dashboard, "load_config", None)))

    def test_next_account_id_reads_config_accounts(self):
        # Before the fix the NameError was swallowed, so config-only accounts
        # were ignored and the suggestion collided with an existing account.
        cfg = {"providers": {"zzreview": {"accounts": {"zzreview-account-7": {}}}}}
        with mock.patch.object(dashboard, "load_config", return_value=cfg):
            suggested = dashboard.wizard_manager.next_account_id("zzreview")
        self.assertEqual(suggested, "zzreview-account-8")


class TestHostHeaderValidation(_ServerMixin, unittest.TestCase):
    """DNS rebinding: a rebinding page must not be able to read /api/token."""

    def test_helper(self):
        for ok in ("127.0.0.1:3333", "localhost:3333", "localhost", "[::1]:3333", None):
            self.assertTrue(is_allowed_host_header(ok), ok)
        for bad in ("evil.example:3333", "evil.example", "127.0.0.1.evil.example",
                    "localhost.evil.example:80", "", "[::2]:3333"):
            self.assertFalse(is_allowed_host_header(bad), bad)

    def test_rebinding_host_is_rejected_for_token(self):
        status, body = self._raw("GET", "/api/token", host=f"attacker.example:{self.port}")
        self.assertEqual(status, 403)
        self.assertNotIn(self.token, body)

    def test_rebinding_host_is_rejected_for_post(self):
        status, _ = self._raw("POST", "/api/route", host="attacker.example",
                              headers={"Content-Length": "2"}, body=b"{}")
        self.assertEqual(status, 403)

    def test_loopback_host_still_works(self):
        status, body = self._raw("GET", "/api/token", host=f"localhost:{self.port}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["token"], self.token)


class TestRequestBodyLimits(_ServerMixin, unittest.TestCase):
    def test_oversized_content_length_is_413_without_reading(self):
        status, body = self._raw(
            "POST", "/api/route",
            headers={"Content-Length": str(MAX_REQUEST_BODY_BYTES + 1), "Content-Type": "application/json"},
        )
        self.assertEqual(status, 413)
        self.assertIn("Payload Too Large", body)

    def test_invalid_content_length_is_400(self):
        for bad in ("abc", "-5"):
            status, _ = self._raw("POST", "/api/route", headers={"Content-Length": bad})
            self.assertEqual(status, 400, bad)

    def test_patch_is_bounded_too(self):
        status, _ = self._raw(
            "PATCH", "/api/models/x",
            headers={"Content-Length": str(MAX_REQUEST_BODY_BYTES + 1)},
        )
        self.assertEqual(status, 413)

    def test_non_object_json_does_not_crash(self):
        body = b"[1, 2, 3]"
        status, resp = self._raw(
            "POST", "/api/route",
            headers={"Content-Length": str(len(body)), "Content-Type": "application/json"},
            body=body,
        )
        self.assertEqual(status, 200, resp)


class TestStaticContainment(_ServerMixin, unittest.TestCase):
    def test_sibling_directory_with_shared_prefix_is_blocked(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            static = tmp / "static"
            static.mkdir()
            (static / "ok.js").write_text("ok", encoding="utf-8")
            evil = tmp / "static_evil"
            evil.mkdir()
            (evil / "secret.txt").write_text("TOPSECRET", encoding="utf-8")
            with mock.patch.object(dashboard, "STATIC_DIR", static.resolve()):
                status, body = self._raw("GET", "/static/../static_evil/secret.txt")
                self.assertEqual(status, 403)
                self.assertNotIn("TOPSECRET", body)
                status, body = self._raw("GET", "/static/ok.js")
                self.assertEqual(status, 200)
                self.assertEqual(body, "ok")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestHandoffRecordTraversal(_ServerMixin, unittest.TestCase):
    def test_path_separators_rejected(self):
        for name in ("../../../etc/x.json", "..%2F..%2Fx.json", "sub/x.json", ".hidden.json", "a\\b.json"):
            status, _ = self._raw("GET", f"/api/handoff/record?filename={name}", headers=self._auth())
            self.assertEqual(status, 400, name)

    def test_name_alias_is_accepted(self):
        # The UI sends ?name=; the server previously only read ?filename=.
        with mock.patch.object(dashboard.handoff_manager, "get_record_by_name", return_value={"x": 1}) as m:
            status, _ = self._raw("GET", "/api/handoff/record?name=handoff_1.json", headers=self._auth())
        self.assertEqual(status, 200)
        m.assert_called_once_with("handoff_1.json")


class TestKiroAutoImportRequiresAuth(_ServerMixin, unittest.TestCase):
    def test_not_public(self):
        self.assertFalse(is_public_path("/api/oauth/kiro/auto-import"))
        self.assertTrue(is_public_path("/api/oauth/cline/callback"))

    def test_unauthenticated_request_is_401(self):
        with mock.patch.object(dashboard.subprocess, "run") as run:
            status, _ = self._raw("GET", "/api/oauth/kiro/auto-import")
        self.assertEqual(status, 401)
        run.assert_not_called()


class TestJsLiteral(unittest.TestCase):
    def test_script_breakout_is_neutralised(self):
        payload = "</script><script>alert(1)</script>"
        out = _js_literal(payload)
        self.assertNotIn("<", out)
        self.assertNotIn(">", out)
        self.assertEqual(json.loads(out), payload)

    def test_quotes_and_backslashes_stay_inside_the_literal(self):
        payload = "a'\\\"&b"
        out = _js_literal(payload)
        self.assertEqual(json.loads(out), payload)
        self.assertTrue(out.startswith('"') and out.endswith('"'))

    def test_callback_pages_do_not_html_escape_into_js(self):
        src = DASHBOARD_PY.read_text(encoding="utf-8")
        self.assertNotIn("wizard_id: '{html.escape(state)}'", src)
        self.assertNotIn("email: {json.dumps(user_email)}", src)


class TestTrustedLocalFile(unittest.TestCase):
    def test_world_writable_file_is_untrusted(self):
        fd, name = tempfile.mkstemp()
        os.close(fd)
        p = Path(name)
        try:
            os.chmod(p, 0o600)
            self.assertTrue(_is_trusted_local_file(p))
            os.chmod(p, 0o666)
            self.assertFalse(_is_trusted_local_file(p))
        finally:
            p.unlink()
        self.assertFalse(_is_trusted_local_file(p))


class TestWizardErrorsDoNotChain(unittest.TestCase):
    """B904: wizard errors built from caught exceptions use `from None`."""

    def test_raise_wizard_error_in_except_uses_from(self):
        tree = ast.parse(DASHBOARD_PY.read_text(encoding="utf-8"))
        offenders = []
        for handler in ast.walk(tree):
            if not isinstance(handler, ast.ExceptHandler) or handler.name is None:
                continue
            for node in ast.walk(handler):
                if (
                    isinstance(node, ast.Raise)
                    and isinstance(node.exc, ast.Call)
                    and getattr(node.exc.func, "id", None) == "WizardError"
                    and node.cause is None
                ):
                    offenders.append(node.lineno)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
