"""
Drift guard: every slash command documented in the README's command table must
exist in BUILTINS, and every non-protected BUILTIN worth surfacing should be
mentioned. Keeps docs honest as commands are added/removed.
"""
import re
from pathlib import Path

from coding_agent.core.commands import BUILTINS

README = Path(__file__).resolve().parents[1] / "README.md"


def _readme_commands() -> set[str]:
    """Extract /command tokens from the README's 'Slash commands' table."""
    text = README.read_text(encoding="utf-8")
    # only look at the slash-commands section to avoid matching URLs/paths
    start = text.find("### Slash commands")
    assert start != -1, "README lost its 'Slash commands' section"
    section = text[start:text.find("\n## ", start)]
    # every `/word` appearing in a backtick span within the table
    return {m.group(1) for m in re.finditer(r"`/([a-z-]+)", section)}


def test_documented_commands_exist_in_builtins():
    documented = _readme_commands()
    assert documented, "no commands parsed from README table"
    missing = sorted(c for c in documented if c not in BUILTINS)
    assert not missing, f"README documents commands not in BUILTINS: {missing}"


def test_core_commands_are_documented():
    # a representative set that must never silently drop out of the docs
    must_have = {"help", "model", "compact", "doctor", "permissions", "vim",
                 "mcp", "hooks", "undo", "recap"}
    documented = _readme_commands()
    missing = sorted(must_have - documented)
    assert not missing, f"README is missing core commands: {missing}"
