"""Tests for engine.py pure functions — no external dependencies needed."""
from sandbox.engine import (
    _parse_action_response,
    _extract_pending_intent,
    _format_pending_intent,
    _is_character_unavailable,
)
from sandbox.engine import SandboxEngine


# ═══════════════════════════════════════════════════════════════
# _parse_action_response
# ═══════════════════════════════════════════════════════════════

class TestParseActionResponse:
    def test_empty_input(self):
        assert _parse_action_response("") is None
        assert _parse_action_response(None) is None

    def test_bare_json_object(self):
        text = '{"selected_actions": [{"character_id": "1", "action_id": "idle_1", "reason": "nothing to do"}]}'
        result = _parse_action_response(text)
        assert result == [
            {"character_id": "1", "action_id": "idle_1", "reason": "nothing to do"},
        ]

    def test_json_in_code_block(self):
        text = '```json\n{"selected_actions": [{"character_id": "2", "action_id": "eat_2", "reason": "hungry"}]}\n```'
        result = _parse_action_response(text)
        assert result == [
            {"character_id": "2", "action_id": "eat_2", "reason": "hungry"},
        ]

    def test_bare_json_array(self):
        text = '[{"character_id": "1", "action_id": "idle_1", "reason": ""}]'
        result = _parse_action_response(text)
        assert result == [
            {"character_id": "1", "action_id": "idle_1", "reason": ""},
        ]

    def test_code_block_without_json_tag(self):
        text = '```\n{"selected_actions": []}\n```'
        result = _parse_action_response(text)
        assert result == []

    def test_malformed_json(self):
        assert _parse_action_response("not json at all") is None
        assert _parse_action_response('{"broken": ') is None


# ═══════════════════════════════════════════════════════════════
# _extract_pending_intent
# ═══════════════════════════════════════════════════════════════

class TestExtractPendingIntent:
    def test_no_action_returns_full_voice(self):
        """When no action was taken, the entire inner voice becomes pending intent."""
        voice = "我想先洗个澡，然后去找真鳕聊天，看看她今天过得怎么样"
        result = _extract_pending_intent(voice, "（未行动）")
        assert result == voice

    def test_short_voice_no_action_is_ignored(self):
        """Too short to be meaningful across rounds."""
        result = _extract_pending_intent("饿了", "（未行动）")
        assert result == ""

    def test_single_step_with_action_returns_empty(self):
        """Single wish that was acted on — done, no pending intent."""
        voice = "太饿了，得赶紧吃点东西"
        result = _extract_pending_intent(voice, "走到厨房；吃剩菜")
        assert result == ""

    def test_chain_word_detection(self):
        """"先...然后..." pattern means multi-step plan — keep as pending."""
        voice = "先吃点东西垫垫肚子，然后去找斑聊聊天"
        result = _extract_pending_intent(voice, "走到厨房；吃剩菜")
        assert result == voice

    def test_chain_word_variants(self):
        """All chain words should trigger pending intent preservation."""
        for kw in ["然后", "再", "接着", "之后", "完了", "顺便"]:
            # Must be >= _MIN_INTENT_LENGTH (10 chars)
            voice = f"先把手洗干净{kw}去卧室好好睡一觉"
            result = _extract_pending_intent(voice, "洗了手")
            assert result == voice, f"failed for chain word: {kw}"

    def test_empty_input(self):
        assert _extract_pending_intent("", "") == ""
        assert _extract_pending_intent("", "做了某事") == ""


# ═══════════════════════════════════════════════════════════════
# _format_pending_intent
# ═══════════════════════════════════════════════════════════════

class TestFormatPendingIntent:
    def test_non_empty_intent(self):
        result = _format_pending_intent("先洗澡然后去找真鳕")
        assert "你上轮想做但还没完成的事" in result
        assert "先洗澡然后去找真鳕" in result
        assert "再想想" in result

    def test_empty_intent(self):
        assert _format_pending_intent("") == ""
        assert _format_pending_intent("   ") == ""


# ═══════════════════════════════════════════════════════════════
# _is_character_unavailable
# ═══════════════════════════════════════════════════════════════

class FakeChar:
    def __init__(self, mood="", current_action=""):
        self.mood = mood
        self.current_action = current_action


class TestIsCharacterUnavailable:
    def test_asleep_by_mood(self):
        assert _is_character_unavailable(FakeChar(mood="Asleep"))

    def test_asleep_by_action(self):
        assert _is_character_unavailable(FakeChar(current_action="sleep"))
        assert _is_character_unavailable(FakeChar(current_action="Sleeping"))

    def test_available(self):
        assert not _is_character_unavailable(FakeChar(mood="Happy", current_action="eat"))
        assert not _is_character_unavailable(FakeChar(mood="Fine", current_action=""))
        assert not _is_character_unavailable(FakeChar())

    def test_case_insensitive(self):
        assert _is_character_unavailable(FakeChar(mood="ASLEEP"))
        assert _is_character_unavailable(FakeChar(current_action="SLEEP"))


# ═══════════════════════════════════════════════════════════════
# SandboxEngine._extract_auto_rule_id (static method)
# ═══════════════════════════════════════════════════════════════

class TestExtractAutoRuleId:
    def test_standard_custom_action(self):
        """custom_{rule_id}_{actor_sim_id}_{target} → rule_id"""
        result = SandboxEngine._extract_auto_rule_id(
            "custom_clean_dishes_12345678901234567_obj_sink_01"
        )
        assert result == "clean_dishes"

    def test_rule_id_with_underscores(self):
        """Rule IDs like 'turn_on_stereo' should be preserved."""
        result = SandboxEngine._extract_auto_rule_id(
            "custom_turn_on_stereo_12345678901234567_obj_stereo_01"
        )
        assert result == "turn_on_stereo"

    def test_non_custom_action_returns_empty(self):
        assert SandboxEngine._extract_auto_rule_id("") == ""
        assert SandboxEngine._extract_auto_rule_id("auto_need_hunger_12345_obj_fridge") == ""
        assert SandboxEngine._extract_auto_rule_id("ai_selected_walk_12345") == ""

    def test_no_numeric_anchor_fallback(self):
        """If no sim_id segment found, fall back to everything after custom_."""
        result = SandboxEngine._extract_auto_rule_id(
            "custom_simple_rule_no_sim_id"
        )
        assert result == "simple_rule_no_sim_id"
