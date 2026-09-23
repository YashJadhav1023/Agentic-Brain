"""Shared test support helpers.

Importing this package pins hermetic defaults (a fixture provider config and a
throwaway dashboard auth token) before any test module loads. See
tests/support/hermetic.py.
"""
from tests.support.hermetic import install as _install_hermetic_defaults

_install_hermetic_defaults()
