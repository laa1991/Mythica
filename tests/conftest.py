"""Shared fixtures for engine module tests.

engine.py imports from mythica_lib (external) and many sibling sandbox
modules.  The showcase repo only includes a curated subset — this conftest
mocks the missing pieces so pure functions can be tested in isolation.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock

# Make the showcase root importable so "import sandbox" works
_showcase_root = Path(__file__).parent.parent
sys.path.insert(0, str(_showcase_root))

# ── Mock mythica_lib (external dependency, not in showcase) ──
_ml = MagicMock()
_ml.ai = MagicMock()
_ml.ai.call_ai = MagicMock()
_ml.ai.AICallParams = MagicMock()
sys.modules["mythica_lib"] = _ml
sys.modules["mythica_lib.ai"] = _ml.ai

# ── Mock sandbox sub-modules that engine imports but aren't tested here ──
# Each gets a MagicMock so "from .xxx import YYY" resolves without error.

_mock_modules = {
    # NOTE: Do NOT mock "sandbox" itself — it must be the real package
    # so that "from sandbox.engine import ..." works.
    "sandbox.world_state": MagicMock(),
    "sandbox.error_log": MagicMock(),
    "sandbox.action_catalog": MagicMock(),
    "sandbox.prompts_sandbox": MagicMock(),
    "sandbox.settings": MagicMock(),
    "sandbox.push_history": MagicMock(),
    "sandbox.action_lifecycle": MagicMock(),
    "sandbox.push_gate": MagicMock(),
    "sandbox.custom_actions": MagicMock(),
    "sandbox.catalog_context": MagicMock(),
    "sandbox.autonomous_observer": MagicMock(),
    "sandbox.actions": MagicMock(),
    "sandbox.actions.rule_schema": MagicMock(),
}

for name, mod in _mock_modules.items():
    sys.modules[name] = mod

# Populate commonly imported names so "from .xxx import YYY" works
sandbox_world_state = sys.modules["sandbox.world_state"]
sandbox_world_state.WorldState = MagicMock()
sandbox_world_state._is_idle_action = lambda action: not action or action.strip() == ""
sandbox_world_state._object_tags = MagicMock(return_value="")

sandbox_error_log = sys.modules["sandbox.error_log"]
sandbox_error_log.log_exception = MagicMock()
sandbox_error_log.log_message = MagicMock()

sandbox_action_catalog = sys.modules["sandbox.action_catalog"]
sandbox_action_catalog.ActionOption = MagicMock()
sandbox_action_catalog.generate_action_catalog = MagicMock(return_value=[])
sandbox_action_catalog.format_catalog_for_prompt = MagicMock(return_value="")
sandbox_action_catalog.build_intent_lookup = MagicMock(return_value={})
sandbox_action_catalog.MAX_OBJECT_ACTIONS_PER_CHAR = 10

sandbox_prompts = sys.modules["sandbox.prompts_sandbox"]
sandbox_prompts.INNER_VOICE_SYSTEM = ""
sandbox_prompts.INNER_VOICE_USER = ""
sandbox_prompts.ACTION_SELECTOR_SYSTEM = ""
sandbox_prompts.ACTION_SELECTOR_USER = ""

sandbox_settings = sys.modules["sandbox.settings"]
sandbox_settings.get_sandbox_connection_settings = MagicMock(return_value=None)
