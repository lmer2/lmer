"""Test consistency across all documentation."""
import re
import pytest


class TestConsistency:
    """Test that information is consistent across files."""

    def test_repository_allow_list_documented(self, rules_dir):
        """Verify repository allow list documentation exists."""
        git_rules = rules_dir / "git.md"
        git_content = git_rules.read_text()

        # Check allow list section exists
        assert "Repository Allow List" in git_content, \
            "Repository Allow List section missing from git.md"

        # Verify it mentions the env var configuration
        assert "LMER_PUSH_ALLOW_LIST" in git_content, \
            "LMER_PUSH_ALLOW_LIST not documented in git.md"

    def test_critical_rules_match(self, main_config, all_rule_files):
        """Verify critical rules in quick reference exist in rule files."""
        main_content = main_config.read_text()

        # Extract quick reference section
        quick_ref = re.search(
            r'## 🚨 Quick Reference.*?\n(.*?)(?=\n##)',
            main_content,
            re.DOTALL
        )

        if quick_ref:
            quick_rules = quick_ref.group(1)

            # Check each ALWAYS/NEVER rule
            rules = re.findall(r'- \*\*(ALWAYS|NEVER)\*\* (.+)', quick_rules)

            # For each rule, verify it exists somewhere in the rule files
            all_rules_content = ""
            for rule_file in all_rule_files:
                all_rules_content += rule_file.read_text()

            for rule_type, rule_text in rules:
                # Clean up the rule text
                clean_text = rule_text.strip()
                # At least the key words should appear
                key_words = [w for w in clean_text.split() if len(w) > 3][:3]

                for word in key_words:
                    assert word.lower() in all_rules_content.lower(), \
                        f"Quick reference rule '{rule_type} {rule_text}' not found in rule files"

    def test_file_paths_exist(self, project_root):
        """Verify all referenced file paths exist."""
        # Check files referenced in documentation
        all_md_files = list(project_root.glob("*.md")) + list(project_root.glob("rules/*.md"))

        for md_file in all_md_files:
            content = md_file.read_text()

            # Find file references
            file_refs = re.findall(r'`([^`]+\.(?:md|py|yaml|toml|json|sh))`', content)

            for ref in file_refs:
                # Skip if it's a command or example
                if any(skip in ref for skip in ['$', 'rgr', 'pytest', 'git', 'npm', 'python']):
                    continue

                # Skip if it contains spaces (likely a command)
                if ' ' in ref:
                    continue

                # Skip template/placeholder paths (contain { or })
                if '{' in ref or '}' in ref:
                    continue

                # Skip .claude/ paths (user/project config, not in this repo)
                if ref.startswith('.claude/'):
                    continue

                # Skip home directory paths
                if ref.startswith('~'):
                    continue

                # Skip absolute paths
                if ref.startswith('/'):
                    continue

                # Check if it's a relative path we should verify
                if '/' in ref:
                    ref_path = project_root / ref
                    if not ref_path.exists():
                        # Try from rules dir
                        ref_path = project_root / "rules" / ref

                    # Only assert for our own files
                    if "Agents/global" in str(ref_path) or ref.startswith("rules/"):
                        assert ref_path.exists(), \
                            f"Referenced file {ref} in {md_file.name} does not exist"
