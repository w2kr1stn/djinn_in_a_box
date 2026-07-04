import re
from io import StringIO

from rich.console import Console

from djinn_in_a_box.core import console as console_mod
from djinn_in_a_box.core.theme import DJINN_THEME


def test_rule_renders_title_to_err_console(monkeypatch) -> None:
    output = StringIO()
    test_console = Console(
        file=output,
        force_terminal=True,
        no_color=True,
        theme=DJINN_THEME,
        width=40,
    )
    monkeypatch.setattr(console_mod, "err_console", test_console)

    console_mod.rule("Environment")

    rendered = output.getvalue()
    rendered_plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
    lines = rendered_plain.splitlines()
    assert lines[0] == ""
    assert lines[1].startswith("─ Environment ─")
    assert len(lines[1]) == 40
