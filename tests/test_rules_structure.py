"""Test rule file structure and consistency."""
import re
import pytest


class TestRuleStructure:
    """Test that all rule files follow consistent structure."""

    def test_all_rules_have_critical_section(self, all_rule_files):
        """Ensure all rule files have a critical section."""
        for rule_file in all_rule_files:
            content = rule_file.read_text()
            assert "## 🚨 Critical" in content, \
                f"{rule_file.name} missing critical section"

    def test_critical_rules_format(self, all_rule_files):
        """Check critical rules use ALWAYS/NEVER format."""
        for rule_file in all_rule_files:
            content = rule_file.read_text()
            # Extract critical section
            critical_match = re.search(
                r'## 🚨 Critical.*?\n(.*?)(?=\n##|\Z)',
                content,
                re.DOTALL
            )
            if critical_match:
                critical_content = critical_match.group(1)
                lines = critical_content.strip().split('\n')
                for line in lines:
                    if line.strip() and line.startswith('-'):
                        # Check for ALWAYS/NEVER in any format (bold or not)
                        assert 'ALWAYS' in line.upper() or 'NEVER' in line.upper(), \
                            f"Critical rule in {rule_file.name} missing ALWAYS/NEVER: {line}"

    def test_rules_referenced_in_main(self, main_config, all_rule_files):
        """Verify all rule files are referenced in AGENTS.md."""
        main_content = main_config.read_text()
        for rule_file in all_rule_files:
            expected_ref = f"rules/{rule_file.name}"
            assert expected_ref in main_content, \
                f"{rule_file.name} not referenced in AGENTS.md"

    def test_markdown_headers_valid(self, all_rule_files):
        """Check markdown headers are properly formatted."""
        for rule_file in all_rule_files:
            content = rule_file.read_text()
            lines = content.split('\n')

            # First line should be # Title
            assert lines[0].startswith('# '), \
                f"{rule_file.name} should start with # header"

            # Check for proper header hierarchy
            header_levels = []
            for line in lines:
                if line.strip().startswith('#'):
                    level = len(line.split()[0])
                    header_levels.append(level)

            # Headers should not skip levels
            for i in range(1, len(header_levels)):
                assert header_levels[i] <= header_levels[i-1] + 1, \
                    f"{rule_file.name} has improper header hierarchy"
