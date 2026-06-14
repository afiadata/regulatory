"""Layer 1 unit tests for system prompt assembly."""

from __future__ import annotations

from datetime import date
from pathlib import Path


class TestAssembleSystemPrompt:
    def test_substitutes_corpus_dates(self) -> None:
        from regulatory.agent.prompts import assemble_system_prompt

        result = assemble_system_prompt(
            corpus_start=date(2024, 1, 1),
            corpus_end=date(2026, 6, 1),
            rule_version="1.0",
            tool_calls_per_turn=8,
        )
        assert "2024-01-01" in result
        assert "2026-06-01" in result

    def test_substitutes_rule_version(self) -> None:
        from regulatory.agent.prompts import assemble_system_prompt

        result = assemble_system_prompt(
            corpus_start=None,
            corpus_end=None,
            rule_version="test-rule-version-xyz",
            tool_calls_per_turn=8,
        )
        assert "test-rule-version-xyz" in result

    def test_substitutes_tool_calls_per_turn(self) -> None:
        from regulatory.agent.prompts import assemble_system_prompt

        result = assemble_system_prompt(
            corpus_start=None,
            corpus_end=None,
            rule_version="1.0",
            tool_calls_per_turn=5,
        )
        assert "5" in result

    def test_none_dates_become_unknown(self) -> None:
        from regulatory.agent.prompts import assemble_system_prompt

        result = assemble_system_prompt(
            corpus_start=None,
            corpus_end=None,
            rule_version="1.0",
            tool_calls_per_turn=8,
        )
        assert "unknown" in result

    def test_includes_all_four_refusal_templates(self) -> None:
        from regulatory.agent.prompts import assemble_system_prompt

        result = assemble_system_prompt(
            corpus_start=date(2024, 1, 1),
            corpus_end=date(2026, 6, 1),
            rule_version="1.0",
            tool_calls_per_turn=8,
        )
        assert "can't advise on what medication" in result
        assert "can't offer legal guidance" in result
        assert "My knowledge covers recalls and supply-chain data" in result
        assert "regulatory analyst assistant" in result

    def test_includes_synthetic_data_caveat_instruction(self) -> None:
        from regulatory.agent.prompts import assemble_system_prompt

        result = assemble_system_prompt(
            corpus_start=date(2024, 1, 1),
            corpus_end=date(2026, 6, 1),
            rule_version="1.0",
            tool_calls_per_turn=8,
        )
        assert "synthetic procurement data" in result

    def test_includes_untrusted_content_guard(self) -> None:
        from regulatory.agent.prompts import assemble_system_prompt

        result = assemble_system_prompt(
            corpus_start=None,
            corpus_end=None,
            rule_version="1.0",
            tool_calls_per_turn=8,
        )
        assert "untrusted_content" in result
        assert "not instructions" in result

    def test_includes_citation_requirement(self) -> None:
        from regulatory.agent.prompts import assemble_system_prompt

        result = assemble_system_prompt(
            corpus_start=None,
            corpus_end=None,
            rule_version="1.0",
            tool_calls_per_turn=8,
        )
        assert "[doc:" in result or "doc:uuid" in result or "[doc" in result

    def test_template_file_exists(self) -> None:
        template = Path(__file__).parents[2] / "src" / "regulatory" / "agent" / "templates" / "system_prompt.md"
        assert template.exists()
        assert template.stat().st_size > 500
