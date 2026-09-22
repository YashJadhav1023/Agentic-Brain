"""Antigravity keyring shielding must hold on every execution path.

Antigravity resolves credentials through `ChainedAuth -> keyringAuth`. There is no
separate file-based auth provider: the per-profile `antigravity-oauth-token` is the
keyring library's *file fallback*, consulted only when the OS keyring cannot be
reached. gnome-keyring runs under `user@1000.service` and holds whatever Google
account signed in last, so without shielding every `--app_data_dir` profile
authenticates as that single identity and the account pool is nominal rather than
real capacity.

Verified empirically on 2026-09-22 against the real CLI:

* `DBUS_SESSION_BUS_ADDRESS=/dev/null` (a bogus *value*) shields correctly —
  profile `antigravity-account-2078` authenticated as its own account identity
  rather than the signed-in account held in the OS keyring.
* **Unsetting** the variable does NOT shield. The D-Bus client reconstructs the
  session bus address from `XDG_RUNTIME_DIR`, so the keyring is reached anyway and
  the profile silently falls back to the signed-in identity.

That distinction is the whole mechanism, so these tests pin it. A refactor that
"cleans up" the `/dev/null` assignment into a `del env[...]` would reintroduce the
bug while still looking correct.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

from agents.antigravity.adapter import AntigravityAccountAdapter, AntigravityAccountConfig

ADAPTER_SOURCE = Path("agents/antigravity/adapter.py")


def _adapter() -> AntigravityAccountAdapter:
    return AntigravityAccountAdapter(
        AntigravityAccountConfig(
            agent_id="antigravity",
            account_id="account-1",
            data_dir="antigravity-account-1",
            command="agy",
        )
    )


class TestKeyringShieldValue(unittest.TestCase):
    def test_shield_sets_a_bogus_value_rather_than_removing_the_variable(self):
        env = _adapter()._subprocess_env()
        self.assertIn(
            "DBUS_SESSION_BUS_ADDRESS",
            env,
            "the variable must be present with a bogus value; unsetting it lets D-Bus "
            "rebuild the address from XDG_RUNTIME_DIR and the keyring wins",
        )
        value = env["DBUS_SESSION_BUS_ADDRESS"]
        self.assertTrue(value, "an empty value is equivalent to unsetting and does not shield")
        self.assertEqual(value, "/dev/null")

    def test_shield_does_not_discard_the_rest_of_the_environment(self):
        # The child still needs PATH to find the binary.
        env = _adapter()._subprocess_env()
        self.assertIn("PATH", env)


class TestEverySubprocessPathIsShielded(unittest.TestCase):
    """A new code path that forgets the shield reintroduces silent identity collapse."""

    def _subprocess_calls(self):
        tree = ast.parse(ADAPTER_SOURCE.read_text(encoding="utf-8"))
        calls = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in {"run", "Popen"}
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
            ):
                calls.append(node)
        return calls

    def test_adapter_has_subprocess_calls_to_check(self):
        # Guards against the AST walk silently finding nothing and passing vacuously.
        self.assertGreater(len(self._subprocess_calls()), 0)

    def test_every_subprocess_call_passes_the_shielded_env(self):
        offenders = []
        for call in self._subprocess_calls():
            kwargs = {kw.arg: kw.value for kw in call.keywords}
            env_arg = kwargs.get("env")
            shielded = (
                isinstance(env_arg, ast.Call)
                and isinstance(env_arg.func, ast.Attribute)
                and env_arg.func.attr == "_subprocess_env"
            )
            if not shielded:
                offenders.append(call.lineno)
        self.assertEqual(
            offenders,
            [],
            f"subprocess call(s) at line(s) {offenders} in {ADAPTER_SOURCE} do not pass "
            "env=self._subprocess_env(); that profile would authenticate as the "
            "signed-in keyring identity instead of its own account",
        )


if __name__ == "__main__":
    unittest.main()
