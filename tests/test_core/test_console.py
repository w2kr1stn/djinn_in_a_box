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


def _recording(width: int = 40) -> tuple[Console, StringIO]:
    output = StringIO()
    return (
        Console(file=output, force_terminal=True, no_color=True, theme=DJINN_THEME, width=width),
        output,
    )


def test_print_captured_does_not_rewrap_long_lines(monkeypatch) -> None:
    """soft_wrap is what keeps a long path from being broken mid-token."""
    test_console, output = _recording(width=40)
    monkeypatch.setattr(console_mod, "err_console", test_console)

    long_path = "/home/dev/" + "a" * 80 + "/file.txt"
    console_mod.print_captured(long_path + "\n")

    assert long_path in output.getvalue()


def test_print_captured_keeps_bracketed_tokens(monkeypatch) -> None:
    test_console, output = _recording()
    monkeypatch.setattr(console_mod, "err_console", test_console)

    console_mod.print_captured("#8 [dev 3/25] RUN apt-get update\n")

    assert "[dev 3/25]" in output.getvalue()


def test_print_captured_routes_to_stdout_when_asked(monkeypatch) -> None:
    """`err=False` must reach stdout — otherwise `djinn mcp servers | grep` breaks."""
    out_console, out_buf = _recording()
    err_console_, err_buf = _recording()
    monkeypatch.setattr(console_mod, "console", out_console)
    monkeypatch.setattr(console_mod, "err_console", err_console_)

    console_mod.print_captured("payload\n", err=False)

    assert "payload" in out_buf.getvalue()
    assert "payload" not in err_buf.getvalue()
