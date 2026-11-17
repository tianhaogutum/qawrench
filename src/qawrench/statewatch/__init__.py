"""An opt-in pytest observer for state that survives a test's setup, call and teardown.

The pytest plugin (``qawrench.statewatch.plugin``, registered as ``statewatch``)
stays inactive until ``--statewatch POLICY.json`` is passed. This package
imports only the standard library: the observation dependencies (DeepDiff,
filelock, pytest-replay) are loaded by the plugin once a policy is given.
"""

from qawrench import __version__

from .policy import Policy, PolicyError

__all__ = ["Policy", "PolicyError", "__version__"]
