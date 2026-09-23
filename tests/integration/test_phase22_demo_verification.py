"""Phase 22 Part 17 — Automated demo verification.

Part 17 asked for interactive/manual demo verification of the Account Add
Wizard. This module performs that walkthrough **automatically** against a real
loopback HTTP server instead of a human clicking through a browser: every step a
demo operator would take is issued as a real authenticated request, and the
resulting lifecycle transitions and live events are asserted.

What is covered, end to end:

* the full 8-step happy path, start -> complete, reaching ``ONLINE``;
* ``Back``, ``Cancel`` and ``Retry``, the three controls the wizard UI exposes;
* cancellation rollback leaving no orphaned account, credential, or directory;
* the live event feed carrying the documented lifecycle event names, tied
  together by one correlation id;
* Bearer-token enforcement on every mutating step;
* duplicate-id rejection.

Two deliberate constraints keep this safe to run in the regression gate:

* validation is driven with ``live=False``, so no request is ever made to a real
  provider with a real key;
* every account id is a unique throwaway, and ``tearDown`` removes any account,
  credential, or directory the flow created, so the local registry is left
  exactly as it was found.

What this does NOT cover, stated plainly: browser rendering, JavaScript
behaviour, CSS layout, and SSE reconnection in a real browser. Those require a
DOM and are outside an HTTP-level check.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
import uuid

from providers.registry.credential_manager import get_credential_manager
from ui.dashboard import dashboard

#: A syntactically valid but entirely fake key. Validation runs with live=False,
#: so it is never sent anywhere.
DEMO_API_KEY = "sk-live-PHASE22DEMOONLYNOTAREALKEY0000000"


class _WizardDemoTestCase(unittest.TestCase):
    """Loopback dashboard plus authenticated POST helpers and full cleanup."""

    @classmethod
    def setUpClass(cls):
        cls.server = dashboard.ThreadedHTTPServer(
            ("127.0.0.1", 0), dashboard.MissionControlHandler
        )
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        # The same token the running handler will expect.
        cls.token = dashboard.get_or_create_auth_token()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        self.account_id = f"phase22demo-{uuid.uuid4().hex[:10]}"
        self.provider_id = "openai"
        self._created_refs: list[str] = []

    def tearDown(self) -> None:
        """Leave the local registry and credential store exactly as found."""
        registry = dashboard.registry.account_registry
        if registry.get_account(self.account_id):
            try:
                registry.safe_remove_account(self.account_id)
            except Exception:
                pass
        cm = get_credential_manager()
        for ref in self._created_refs:
            try:
                if cm.exists(ref):
                    cm.delete(ref)
            except Exception:
                pass
        from providers.registry.config import remove_account_config
        try:
            remove_account_config(self.provider_id, self.account_id)
        except Exception:
            pass

    # ---- HTTP helpers ----------------------------------------------------

    def _post(self, path: str, payload: dict, authed: bool = True):
        """POST and return (status, decoded_body)."""
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        if authed:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"raw": raw}

    def _get(self, path: str, authed: bool = True):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        if authed:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))


    # ---- flow helpers ----------------------------------------------------

    def _start(self):
        status, body = self._post(
            "/api/wizard/start",
            {"provider_id": self.provider_id, "account_id": self.account_id},
        )
        self.assertEqual(status, 201, body)
        wizard_id = body["wizard"]["wizard_id"]
        self._created_refs.append(
            f"secret://mission-control/{self.provider_id}/{self.account_id}/api_key"
        )
        return wizard_id, body["wizard"]

    def _step(self, action: str, wizard_id: str, **extra):
        payload = {"wizard_id": wizard_id}
        payload.update(extra)
        return self._post(f"/api/wizard/{action}", payload)

    def _drive_to_ready(self, wizard_id: str) -> None:
        """select-auth -> configure -> authenticate -> validate (offline)."""
        status, body = self._step("select-auth", wizard_id, auth_method="api_key")
        self.assertEqual(status, 200, body)
        status, body = self._step(
            "configure", wizard_id, config={"api_key": DEMO_API_KEY, "model": "gpt-4o"}
        )
        self.assertEqual(status, 200, body)
        status, body = self._step("authenticate", wizard_id)
        self.assertEqual(status, 200, body)
        status, body = self._step("validate", wizard_id, live=False)
        self.assertEqual(status, 200, body)


class TestWizardHappyPathWalkthrough(_WizardDemoTestCase):
    """The walkthrough a demo operator would perform, start to finish."""

    def test_full_eight_step_flow_reaches_online(self):
        wizard_id, initial = self._start()
        self.assertEqual(initial["step"], "select_provider")
        self.assertEqual(initial["lifecycle_state"], "DISCOVERED")

        status, body = self._step("select-auth", wizard_id, auth_method="api_key")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["wizard"]["login_method"], "api_key")

        status, body = self._step(
            "configure", wizard_id, config={"api_key": DEMO_API_KEY, "model": "gpt-4o"}
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["wizard"]["lifecycle_state"], "CONFIGURING")

        status, body = self._step("authenticate", wizard_id)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["wizard"]["lifecycle_state"], "AUTHENTICATED")

        status, body = self._step("validate", wizard_id, live=False)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["wizard"]["lifecycle_state"], "READY")
        self.assertTrue(
            body["wizard"]["discovered_models"], "validation should discover models"
        )

        status, body = self._step("register", wizard_id)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["wizard"]["registered"])

        status, body = self._step("health", wizard_id)
        self.assertEqual(status, 200, body)
        self.assertIn("health", body)

        status, body = self._step("complete", wizard_id)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "complete")
        self.assertTrue(body["wizard"]["completed"])
        self.assertEqual(body["wizard"]["lifecycle_state"], "ONLINE")

    def test_completed_account_is_visible_and_admissible(self):
        """The demo's end state: the account is usable, not merely present."""
        wizard_id, _ = self._start()
        self._drive_to_ready(wizard_id)
        self._step("register", wizard_id)
        self._step("complete", wizard_id)

        listed = self._get("/api/accounts")["accounts"]
        mine = [a for a in listed if a["id"] == self.account_id]
        self.assertEqual(len(mine), 1, "completed account must appear in /api/accounts")
        self.assertEqual(mine[0]["lifecycle_state"], "ONLINE")

        from brain.router.smart_router import SmartRouter

        account = dashboard.registry.account_registry.get_account(self.account_id)
        ok, reason, detail = SmartRouter().check_account_admissible(account)
        self.assertTrue(ok, f"a completed account must be routable: {reason} {detail}")

    def test_completion_increments_the_granular_online_metric(self):
        before = self._get("/api/accounts/metrics")["metrics"]
        wizard_id, _ = self._start()
        self._drive_to_ready(wizard_id)
        self._step("register", wizard_id)
        self._step("complete", wizard_id)
        after = self._get("/api/accounts/metrics")["metrics"]

        self.assertEqual(after["registered"], before["registered"] + 1)
        self.assertEqual(after["online"], before["online"] + 1)

    def test_no_step_response_ever_returns_the_api_key(self):
        wizard_id, _ = self._start()
        status, body = self._step("select-auth", wizard_id, auth_method="api_key")
        status, body = self._step(
            "configure", wizard_id, config={"api_key": DEMO_API_KEY}
        )
        self.assertNotIn(DEMO_API_KEY, json.dumps(body))

        status, body = self._step("authenticate", wizard_id)
        self.assertNotIn(DEMO_API_KEY, json.dumps(body))
        status, body = self._step("validate", wizard_id, live=False)
        self.assertNotIn(DEMO_API_KEY, json.dumps(body))

    def test_the_credential_is_stored_by_reference_only(self):
        wizard_id, _ = self._start()
        self._drive_to_ready(wizard_id)
        self._step("register", wizard_id)

        account = dashboard.registry.account_registry.get_account(self.account_id)
        self.assertTrue(account.credential_reference.startswith("secret://"))
        self.assertNotIn(DEMO_API_KEY, str(account.to_dict()))


class TestWizardControls(_WizardDemoTestCase):
    """Back, Cancel and Retry — the controls the UI exposes at every step."""

    def test_back_returns_to_the_previous_step(self):
        wizard_id, _ = self._start()
        self._step("select-auth", wizard_id, auth_method="api_key")
        status, body = self._step(
            "configure", wizard_id, config={"api_key": DEMO_API_KEY}
        )
        self.assertEqual(body["wizard"]["step"], "configure")

        status, body = self._step("back", wizard_id)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["wizard"]["step"], "select_auth")

    def test_cancel_leaves_no_registered_account(self):
        wizard_id, _ = self._start()
        self._drive_to_ready(wizard_id)

        status, body = self._step("cancel", wizard_id)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "cancelled")
        self.assertTrue(body["wizard"]["cancelled"])
        self.assertIsNone(
            dashboard.registry.account_registry.get_account(self.account_id),
            "a cancelled flow must not leave a registered account",
        )

    def test_cancel_deletes_the_credential_it_created(self):
        """Zero orphans: the stored key must not outlive the cancelled flow."""
        wizard_id, _ = self._start()
        self._drive_to_ready(wizard_id)
        ref = f"secret://mission-control/{self.provider_id}/{self.account_id}/api_key"
        self.assertTrue(get_credential_manager().exists(ref), "precondition")

        self._step("cancel", wizard_id)
        self.assertFalse(
            get_credential_manager().exists(ref),
            "cancellation must delete the credential it stored",
        )

    def test_cancel_after_registration_removes_the_account(self):
        wizard_id, _ = self._start()
        self._drive_to_ready(wizard_id)
        self._step("register", wizard_id)
        self.assertIsNotNone(
            dashboard.registry.account_registry.get_account(self.account_id)
        )

        self._step("cancel", wizard_id)
        self.assertIsNone(
            dashboard.registry.account_registry.get_account(self.account_id),
            "cancelling after register must roll the registration back",
        )

    def test_a_cancelled_session_cannot_be_driven_further(self):
        wizard_id, _ = self._start()
        self._step("cancel", wizard_id)
        status, body = self._step("select-auth", wizard_id, auth_method="api_key")
        self.assertIn(status, (404, 422, 500), body)

    def test_retry_clears_a_failure_so_the_step_can_be_reissued(self):
        wizard_id, _ = self._start()
        # An unsupported method is a real, recoverable failure.
        status, body = self._step("select-auth", wizard_id, auth_method="not-a-method")
        self.assertEqual(status, 422, body)
        self.assertTrue(body["wizard"]["last_error"])

        status, body = self._step("retry", wizard_id)
        self.assertEqual(status, 200, body)
        self.assertIsNone(body["wizard"]["last_error"])

        # And the corrected step now succeeds.
        status, body = self._step("select-auth", wizard_id, auth_method="api_key")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["wizard"]["login_method"], "api_key")


class TestWizardFailureHandling(_WizardDemoTestCase):
    def test_an_unsupported_login_method_is_rejected_with_a_failure_state(self):
        wizard_id, _ = self._start()
        status, body = self._step("select-auth", wizard_id, auth_method="oauth")
        self.assertEqual(status, 422, body)
        self.assertIn("failure_state", body)
        self.assertTrue(body["error"])

    def test_starting_without_required_fields_is_a_400(self):
        status, body = self._post("/api/wizard/start", {"provider_id": "openai"})
        self.assertEqual(status, 400, body)

    def test_a_duplicate_account_id_never_reuses_the_existing_account(self):
        """A second wizard for a taken id must not be able to touch the first.

        Originally the wizard rejected a duplicate with 409. f97fc14 changed
        ``/api/wizard/start`` to auto-increment the id instead ("OmniRoute
        multi-account parity": ``foo`` -> ``foo-1``). The security property both
        behaviours must keep is the same: the new session is never bound to the
        existing account id, and the registered account is left untouched.
        """
        wizard_id, _ = self._start()
        self._drive_to_ready(wizard_id)
        status, body = self._step("register", wizard_id)
        self.assertEqual(status, 200, body)
        registry = dashboard.registry.account_registry
        original = registry.get_account(self.account_id)
        self.assertIsNotNone(original)
        original_state = original.lifecycle_state

        status, body = self._post(
            "/api/wizard/start",
            {"provider_id": self.provider_id, "account_id": self.account_id},
        )
        self.assertIn(status, (201, 409), body)
        if status == 201:
            new_id = body["wizard"]["account_id"]
            self.assertNotEqual(new_id, self.account_id, body)
            self.assertTrue(new_id.startswith(self.account_id.rsplit("-", 1)[0]), new_id)
            # Nothing is registered under the new id until that wizard registers.
            self.assertIsNone(registry.get_account(new_id))
            self._step("cancel", body["wizard"]["wizard_id"])

        still = registry.get_account(self.account_id)
        self.assertIs(still, original)
        self.assertEqual(still.lifecycle_state, original_state)

    def test_an_unknown_wizard_session_is_a_404(self):
        status, body = self._step("configure", "wiz-doesnotexist", config={})
        self.assertEqual(status, 404, body)

    def test_authenticate_without_a_credential_fails_cleanly(self):
        """An API provider with no key must fail, not half-onboard."""
        wizard_id, _ = self._start()
        self._step("select-auth", wizard_id, auth_method="api_key")
        self._step("configure", wizard_id, config={"model": "gpt-4o"})

        status, body = self._step("authenticate", wizard_id)
        self.assertEqual(status, 422, body)
        self.assertEqual(body["failure_state"], "AUTH_FAILED")
        self.assertIsNone(
            dashboard.registry.account_registry.get_account(self.account_id)
        )


class TestWizardRequiresAuthentication(_WizardDemoTestCase):
    """Every mutating step must reject an unauthenticated caller."""

    def test_start_without_a_token_is_unauthorized(self):
        status, body = self._post(
            "/api/wizard/start",
            {"provider_id": self.provider_id, "account_id": self.account_id},
            authed=False,
        )
        self.assertEqual(status, 401, body)

    def test_every_step_action_rejects_an_unauthenticated_caller(self):
        wizard_id, _ = self._start()
        for action in (
            "select-auth",
            "configure",
            "authenticate",
            "validate",
            "register",
            "health",
            "complete",
            "back",
            "retry",
            "cancel",
        ):
            status, body = self._post(
                f"/api/wizard/{action}", {"wizard_id": wizard_id}, authed=False
            )
            self.assertEqual(status, 401, f"{action} must require auth: {body}")

    def test_an_unauthenticated_attempt_creates_no_account(self):
        self._post(
            "/api/wizard/start",
            {"provider_id": self.provider_id, "account_id": self.account_id},
            authed=False,
        )
        self.assertIsNone(
            dashboard.registry.account_registry.get_account(self.account_id)
        )


class TestWizardCapabilityResolution(_WizardDemoTestCase):
    """An onboarded account must be routable, not merely registered.

    The router rejects any account whose declared capabilities contain no member
    of the ``Capability`` enum (``CAPABILITY_MISMATCH``). Before this was fixed,
    the wizard never set capabilities at all, so a demo could walk an account all
    the way to ``ONLINE`` and the router would then silently never use it — the
    exact failure mode Part 12's rejection reasons exist to surface.
    """

    def _register(self, config: dict | None = None):
        wizard_id, _ = self._start()
        self._step("select-auth", wizard_id, auth_method="api_key")
        payload = {"api_key": DEMO_API_KEY}
        payload.update(config or {})
        self._step("configure", wizard_id, config=payload)
        self._step("authenticate", wizard_id)
        self._step("validate", wizard_id, live=False)
        status, body = self._step("register", wizard_id)
        self.assertEqual(status, 200, body)
        return dashboard.registry.account_registry.get_account(self.account_id)

    def test_a_registered_account_never_has_an_empty_capability_set(self):
        account = self._register()
        self.assertTrue(
            account.capabilities, "an account with no capabilities is unroutable"
        )

    def test_default_capabilities_are_all_real_router_capabilities(self):
        from agents.base.adapter import Capability

        account = self._register()
        valid = {c.value for c in Capability}
        for capability in account.capabilities:
            self.assertIn(
                capability, valid, f"'{capability}' is not a Capability the router knows"
            )

    def test_explicit_capabilities_are_honoured(self):
        account = self._register({"capabilities": ["code-review", "documentation"]})
        self.assertEqual(account.capabilities, ["code-review", "documentation"])

    def test_unrecognised_capabilities_are_discarded_not_stored(self):
        """Storing a string the router ignores would look configured yet be dead."""
        account = self._register({"capabilities": ["chat", "streaming"]})
        for bogus in ("chat", "streaming"):
            self.assertNotIn(bogus, account.capabilities)
        self.assertTrue(account.capabilities, "must fall back rather than store junk")

    def test_a_partially_valid_capability_list_keeps_only_the_valid_entries(self):
        account = self._register({"capabilities": ["code-review", "not-a-capability"]})
        self.assertEqual(account.capabilities, ["code-review"])

    def test_an_api_account_does_not_claim_local_execution_capabilities(self):
        """A remote API key cannot run a shell, so it must not advertise one."""
        account = self._register()
        for local_only in (
            "terminal-operations",
            "kubernetes-read-only",
            "cloud-read-only",
        ):
            self.assertNotIn(local_only, account.capabilities)


class TestWizardLiveEventFeed(_WizardDemoTestCase):
    """The live feed a demo operator watches while the wizard runs."""

    def _my_events(self, correlation_id: str):
        return [
            e
            for e in self._get("/api/wizard/events")["events"]
            if e.get("correlation_id") == correlation_id
        ]

    def test_the_documented_event_sequence_is_emitted_in_order(self):
        wizard_id, wizard = self._start()
        corr = wizard["correlation_id"]
        self._drive_to_ready(wizard_id)
        self._step("register", wizard_id)
        self._step("health", wizard_id)
        self._step("complete", wizard_id)

        names = [e["event_type"] for e in self._my_events(corr)]
        expected = [
            "account.create_started",
            "account.configuring",
            "account.authentication_started",
            "account.authentication_success",
            "account.validation_started",
            "account.validation_success",
            "account.registered",
            "account.health_check",
        ]
        for name in expected:
            self.assertIn(name, names, f"missing '{name}' in {names}")
        # Order must reflect the real flow, not arbitrary interleaving.
        self.assertLess(
            names.index("account.create_started"), names.index("account.configuring")
        )
        self.assertLess(
            names.index("account.authentication_started"),
            names.index("account.validation_started"),
        )
        self.assertLess(
            names.index("account.validation_success"), names.index("account.registered")
        )

    def test_every_event_of_one_flow_shares_the_correlation_id(self):
        wizard_id, wizard = self._start()
        corr = wizard["correlation_id"]
        self._drive_to_ready(wizard_id)

        events = self._my_events(corr)
        self.assertGreaterEqual(len(events), 4)
        for event in events:
            self.assertEqual(event["correlation_id"], corr)
            self.assertEqual(event["account_id"], self.account_id)

    def test_the_event_feed_never_carries_the_api_key(self):
        wizard_id, wizard = self._start()
        self._drive_to_ready(wizard_id)
        self.assertNotIn(
            DEMO_API_KEY, json.dumps(self._get("/api/wizard/events"))
        )

    def test_a_cancelled_flow_reports_its_own_lifecycle_events(self):
        wizard_id, wizard = self._start()
        corr = wizard["correlation_id"]
        self._drive_to_ready(wizard_id)
        self._step("cancel", wizard_id)

        names = [e["event_type"] for e in self._my_events(corr)]
        self.assertIn("account.create_started", names)
        self.assertTrue(
            any(n.startswith("account.") for n in names),
            "a cancelled flow must still be observable in the feed",
        )

    def test_failures_are_visible_in_the_feed_not_silent(self):
        wizard_id, wizard = self._start()
        corr = wizard["correlation_id"]
        self._step("select-auth", wizard_id, auth_method="api_key")
        self._step("configure", wizard_id, config={"model": "gpt-4o"})
        self._step("authenticate", wizard_id)  # fails: no credential

        names = [e["event_type"] for e in self._my_events(corr)]
        self.assertIn("account.authentication_started", names)
        self.assertNotIn(
            "account.authentication_success",
            names,
            "a failed authentication must not emit success",
        )


if __name__ == "__main__":
    unittest.main()
