"""Regression tests for the Mission Control dashboard security review.

Covers: the undefined `load_config` NameError, DNS-rebinding Host checks,
bounded request bodies, static-file containment, handoff record traversal,
the kiro auto-import auth gate, exception chaining in the wizard, inline-script
escaping for OAuth callback pages.

Nothing here performs a real OAuth login, agent execution or credential write.
"""
from __future__ import annotations

import ast
import base64
import datetime
import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from memory.store.memory_store import MemoryScope
from ui.dashboard import dashboard
from ui.dashboard.dashboard import (
    MAX_REQUEST_BODY_BYTES,
    ThreadedHTTPServer,
    WizardSession,
    _cleanup_stale_worktrees,
    _is_trusted_local_file,
    _js_literal,
    _redact_preserving_booleans,
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

    def test_non_object_and_malformed_json_are_400(self):
        for body in (b"[1, 2, 3]", b"{not json", b"\xff\xfe"):
            status, resp = self._raw(
                "POST", "/api/route",
                headers={"Content-Length": str(len(body)), "Content-Type": "application/json"},
                body=body,
            )
            self.assertEqual(status, 400, (body, resp))

    def test_empty_body_is_still_accepted(self):
        status, _ = self._raw("POST", "/api/route", headers={"Content-Length": "0"})
        self.assertEqual(status, 200)


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


def _post_json(test, path, payload, auth=True, method="POST"):
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Length": str(len(body)), "Content-Type": "application/json"}
    if auth:
        headers.update(test._auth())
    status, resp = test._raw(method, path, headers=headers, body=body)
    try:
        return status, json.loads(resp)
    except ValueError:
        return status, resp


class TestClineCallbackState(_ServerMixin, unittest.TestCase):
    """Login CSRF: the public Cline callback must not register forged accounts."""

    FORGED_CODE = base64.b64encode(json.dumps({"accessToken": "forged", "email": "x@evil"}).encode()).decode()

    def _new_session(self, provider="cline"):
        sess = WizardSession(provider, "zz-review-cline")
        with dashboard.wizard_manager._lock:
            dashboard.wizard_manager._sessions[sess.wizard_id] = sess
        self.addCleanup(dashboard.wizard_manager._sessions.pop, sess.wizard_id, None)
        return sess

    def _callback(self, query):
        with mock.patch.object(dashboard, "add_account_config") as add_cfg, \
                mock.patch.object(dashboard.urllib.request, "urlopen") as urlopen:
            status, body = self._raw("GET", f"/api/oauth/cline/callback?{query}")
        return status, body, add_cfg, urlopen

    def test_missing_state_rejected_without_side_effects(self):
        status, body, add_cfg, urlopen = self._callback(f"code={self.FORGED_CODE}&account_id=zz-evil")
        self.assertEqual(status, 400)
        self.assertIn("Invalid or expired OAuth state", body)
        add_cfg.assert_not_called()
        urlopen.assert_not_called()

    def test_wizard_id_is_not_accepted_as_state(self):
        sess = self._new_session()
        status, _, add_cfg, _ = self._callback(f"code={self.FORGED_CODE}&state={sess.wizard_id}")
        self.assertEqual(status, 400)
        add_cfg.assert_not_called()

    def test_state_is_single_use_expiring_and_provider_bound(self):
        wm = dashboard.wizard_manager
        sess = self._new_session()
        nonce = wm.issue_oauth_state(sess)
        self.assertIs(wm.consume_oauth_state(nonce, "cline"), sess)
        self.assertIsNone(wm.consume_oauth_state(nonce, "cline"))  # single use

        nonce = wm.issue_oauth_state(sess)
        self.assertIsNone(wm.consume_oauth_state(nonce, "antigravity"))  # wrong provider

        nonce = wm.issue_oauth_state(sess)
        with mock.patch.object(dashboard.time, "time", return_value=dashboard.time.time() + 3600):
            self.assertIsNone(wm.consume_oauth_state(nonce, "cline"))  # expired
        self.assertIsNone(wm.consume_oauth_state("", "cline"))

    def test_launch_login_uses_nonce_not_wizard_id(self):
        sess = self._new_session()
        info = dashboard.wizard_manager.launch_login(sess, redirect_origin="http://127.0.0.1:1")
        state = dashboard.urllib.parse.parse_qs(dashboard.urllib.parse.urlsplit(info["auth_url"]).query)["state"][0]
        self.assertNotEqual(state, sess.wizard_id)
        self.assertIs(dashboard.wizard_manager.consume_oauth_state(state, "cline"), sess)


class TestMemoryScope(_ServerMixin, unittest.TestCase):
    def test_add_uses_requested_scope(self):
        entry = mock.Mock()
        entry.to_dict.return_value = {"ok": True}
        with mock.patch.object(dashboard.memory_store, "add", return_value=entry) as add:
            status, _ = _post_json(self, "/api/memory/add", {"content": "x", "scope": "task"})
        self.assertEqual(status, 200)
        self.assertIs(add.call_args.kwargs["scope"], MemoryScope.TASK)

    def test_add_unknown_scope_is_400(self):
        with mock.patch.object(dashboard.memory_store, "add") as add:
            status, _ = _post_json(self, "/api/memory/add", {"content": "x", "scope": "bogus"})
        self.assertEqual(status, 400)
        add.assert_not_called()

    def test_search_scope_filter_is_applied(self):
        with mock.patch.object(dashboard, "MemoryRetriever") as retr:
            retr.return_value.retrieve_context.return_value = []
            status, _ = _post_json(self, "/api/memory/search", {"query": "q", "scope": "agent"})
        self.assertEqual(status, 200)
        self.assertIs(retr.return_value.retrieve_context.call_args.kwargs["scope"], MemoryScope.AGENT)


class TestBadIntegersReturn400(_ServerMixin, unittest.TestCase):
    def test_get_limits(self):
        for path in ("/api/memory/retrieval-history?limit=abc", "/api/routing/history?limit=abc"):
            status, body = self._raw("GET", path, headers=self._auth())
            self.assertEqual(status, 400, path)
            self.assertIn("limit", body)

    def test_memory_importance(self):
        with mock.patch.object(dashboard.memory_store, "add") as add:
            status, body = _post_json(self, "/api/memory/add", {"content": "x", "importance": "high"})
        self.assertEqual(status, 400)
        self.assertIn("importance", body["message"])
        add.assert_not_called()

    def test_patch_account_priority(self):
        reg = dashboard.registry.account_registry
        with mock.patch.object(reg, "get_account", return_value=mock.Mock()), \
                mock.patch.object(reg, "update_account") as upd:
            status, _ = _post_json(self, "/api/accounts/zz-any", {"priority": "high"}, method="PATCH")
        self.assertEqual(status, 400)
        upd.assert_not_called()

    def test_create_account_validates_before_storing_credentials(self):
        with mock.patch.object(dashboard, "get_credential_manager") as gcm, \
                mock.patch.object(dashboard.registry.account_registry, "register_account") as reg:
            status, _ = _post_json(self, "/api/accounts", {
                "name": "zz-review", "provider": "zzprov", "api_key": "k", "concurrency_limit": "many",
            })
        self.assertEqual(status, 400)
        gcm.assert_not_called()
        reg.assert_not_called()

    def test_cleanup_max_age(self):
        status, _ = _post_json(self, "/api/worktrees/cleanup", {"confirm": True, "max_age_hours": "x"})
        self.assertEqual(status, 400)


class _FakeRecord:
    def __init__(self, task_id, status, hours_ago):
        self.task_id = task_id
        self.status = status
        self.updated_at = (datetime.datetime.now(datetime.timezone.utc)
                           - datetime.timedelta(hours=hours_ago)).isoformat()

    def to_dict(self):
        return {"task_id": self.task_id, "status": self.status}


class _FakeWorktrees:
    """Mirrors the real WorktreeManager signatures the dashboard must call."""

    def __init__(self, records):
        self.records = {r.task_id: r for r in records}
        self.cleaned = []

    def get(self, task_id):
        return self.records.get(task_id)

    def status(self, task_id=None):
        return list(self.records.values())

    def cleanup(self, task_id, force=False, delete_branch=False):
        self.cleaned.append((task_id, delete_branch))
        return task_id in self.records

    def recover(self):
        return [r for r in self.records.values() if r.status == "ORPHANED"]


class TestWorktreeEndpoints(_ServerMixin, unittest.TestCase):
    def _fake(self):
        fake = _FakeWorktrees([
            _FakeRecord("old-applied", "APPLIED", 48),
            _FakeRecord("new-applied", "APPLIED", 1),
            _FakeRecord("old-active", "ACTIVE", 48),
            _FakeRecord("orphan", "ORPHANED", 48),
        ])
        patcher = mock.patch.object(dashboard.orchestrator, "_worktree_manager", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def test_batch_cleanup_only_sweeps_stale_finished_worktrees(self):
        fake = self._fake()
        status, body = _post_json(self, "/api/worktrees/cleanup", {"confirm": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(sorted(t for t, _ in fake.cleaned), ["old-applied", "orphan"])
        self.assertTrue(all(db is False for _, db in fake.cleaned))

    def test_single_cleanup_uses_cleanup_signature(self):
        fake = self._fake()
        status, body = _post_json(self, "/api/worktrees/cleanup", {"confirm": True, "task_id": "old-active"})
        self.assertEqual(status, 200, body)
        self.assertEqual(fake.cleaned, [("old-active", True)])

    def test_recover(self):
        self._fake()
        status, body = _post_json(self, "/api/worktrees/recover", {"task_id": "orphan"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["worktree"]["task_id"], "orphan")
        self.assertEqual(body["count"], 1)

    def test_unknown_task_is_404(self):
        self._fake()
        for path in ("/api/worktrees/reject", "/api/worktrees/apply", "/api/worktrees/recover"):
            status, _ = _post_json(self, path, {"task_id": "nope", "confirm": True})
            self.assertEqual(status, 404, path)

    def test_stale_helper_direct(self):
        fake = _FakeWorktrees([_FakeRecord("f", "FAILED", 30), _FakeRecord("p", "PENDING_REVIEW", 300)])
        self.assertEqual(_cleanup_stale_worktrees(fake, 24), [{"task_id": "f", "previous_status": "FAILED"}])


class TestProviderIdValidation(_ServerMixin, unittest.TestCase):
    def test_empty_or_unsafe_id_rejected(self):
        with mock.patch.object(dashboard.registry, "register_ai_provider") as reg:
            for payload in ({}, {"id": ""}, {"id": "../x"}, {"id": 5}, {"id": "a b"}):
                status, _ = _post_json(self, "/api/providers", payload)
                self.assertEqual(status, 400, payload)
        reg.assert_not_called()


class TestCheckAuthBooleans(_ServerMixin, unittest.TestCase):
    def test_helper_keeps_bools_but_redacts_secrets(self):
        out = _redact_preserving_booleans({
            "authenticated": True, "token_exists": False,
            "auth_token": "sk-abcdefghijklmnopqrstuvwxyz0123",
            "nested": [{"token_exists": True}],
        })
        self.assertIs(out["authenticated"], True)
        self.assertIs(out["token_exists"], False)
        self.assertIs(out["nested"][0]["token_exists"], True)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz0123", json.dumps(out))

    def test_get_endpoint_returns_real_booleans(self):
        sess = mock.Mock()
        with mock.patch.object(dashboard.wizard_manager, "get", return_value=sess), \
                mock.patch.object(dashboard.wizard_manager, "check_auth_status",
                                  return_value={"authenticated": True, "token_exists": True, "account_id": "a"}):
            status, body = self._raw("GET", "/api/wizard/check-auth?wizard_id=w", headers=self._auth())
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIs(data["authenticated"], True)
        self.assertIs(data["token_exists"], True)


class TestServerBacklog(unittest.TestCase):
    def test_listen_backlog_is_raised(self):
        self.assertGreaterEqual(ThreadedHTTPServer.request_queue_size, 128)
        self.assertTrue(ThreadedHTTPServer.daemon_threads)


if __name__ == "__main__":
    unittest.main()


class TestExecuteRunsTheRequestedTask(_ServerMixin, unittest.TestCase):
    """"Run now" on one task must run that task, not the next one in the queue."""

    def _patch(self, task):
        started = []
        getter = mock.patch.object(dashboard.task_manager, "get_task", return_value=task)
        now = mock.patch.object(dashboard.orchestrator, "execute_task_now", side_effect=started.append)
        nxt = mock.patch.object(dashboard.orchestrator, "execute_next", side_effect=lambda: started.append("NEXT"))
        for p in (getter, now, nxt):
            p.start()
            self.addCleanup(p.stop)
        return started

    def _wait(self, started):
        for _ in range(100):
            if started:
                return
            threading.Event().wait(0.02)

    def test_task_id_runs_that_task(self):
        task = mock.Mock(task_id="task-abc", status=dashboard.TaskStatus.READY)
        started = self._patch(task)
        status, body = _post_json(self, "/api/execute", {"task_id": "task-abc"})
        self.assertEqual(status, 200, body)
        self._wait(started)
        self.assertEqual(started, [task])

    def test_unknown_task_is_404_and_nothing_runs(self):
        started = self._patch(None)
        status, _ = _post_json(self, "/api/execute", {"task_id": "task-missing"})
        self.assertEqual(status, 404)
        self.assertEqual(started, [])

    def test_non_ready_task_is_409_and_nothing_runs(self):
        task = mock.Mock(task_id="task-done", status=dashboard.TaskStatus.COMPLETED)
        started = self._patch(task)
        status, _ = _post_json(self, "/api/execute", {"task_id": "task-done"})
        self.assertEqual(status, 409)
        self.assertEqual(started, [])

    def test_no_task_id_still_runs_the_queue(self):
        started = self._patch(None)
        status, _ = _post_json(self, "/api/execute", {})
        self.assertEqual(status, 200)
        self._wait(started)
        self.assertEqual(started, ["NEXT"])
