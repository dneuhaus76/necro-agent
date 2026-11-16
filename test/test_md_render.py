"""agent answers are rendered as structured Markdown in the terminal.

Line-based renderer in ui/render.py (md_lines) - no App state, no full Markdown
parser. Covered: headlines (bold, h1 also underlined - no emoji marker, it reads
like a bullet), inline bold/italic/code, lists (bullet, ordered, nested, task
checkboxes), fenced code blocks, quotes and horizontal rules, and tables (inline
markup inside cells, column shrink, visible cut marker).
"""
import re

from modules.ui import render as r

PLAIN = lambda lines: [re.sub(r"\x1b\[[0-9;]*m", "", l) for l in lines]  # noqa: E731


def md(text, width=40):
    return PLAIN(r.md_lines(text, width, r.GREY))


def raw(text, width=40):
    return r.md_lines(text, width, r.GREY)


# ---- headlines
def test_headline_levels_get_bold_and_no_marker():
    out = raw("# Eins\n## Zwei\n### Drei")
    assert ";1" in out[0] and ";4" in out[0]  # h1: bold + underlined
    assert PLAIN(out[:1])[0] == "Eins"  # no emoji/bullet in front of a headline
    assert ";1" in out[1] and ";4" not in out[1]  # h2: bold only
    assert PLAIN(out[1:2])[0] == "Zwei"
    assert PLAIN(out[2:3])[0] == "Drei" and ";1" in out[2]  # deeper levels: bold


def test_headline_uses_the_chocolate_accent():
    """The accent replaces the block grey: a headline must not inherit the base color."""
    out = raw("# Titel\n\nText")
    assert r.CHOCOLATE in out[0] and r.GREY not in out[0]
    assert r.GREY in out[2]  # body text keeps the block color


def test_long_headline_wraps_without_indent():
    lines = md("# Ein sehr langer Titel der ueber die Breite hinausgeht ja")
    assert len(lines) > 1
    assert lines[0].startswith("Ein sehr langer")
    assert " ".join(l.strip() for l in lines) == "Ein sehr langer Titel der ueber die Breite hinausgeht ja"
    assert all(len(l) <= 40 for l in lines)  # wrapped at the width, nothing padded


def test_headline_hash_without_space_is_text():
    assert md("#nicht-headline") == ["#nicht-headline"]


# ---- inline
def test_inline_bold_italic_code_and_link():
    out = raw("a **fett** b *kursiv* c `code` d [text](http://x.y)", 80)
    joined = PLAIN(out)
    assert ";1" in out[0] and ";3" in out[0]
    assert "48;5;236" in out[0]  # code: panel background
    assert joined == ["a fett b kursiv c code d text (http://x.y)"]


# ---- lists
def test_bullet_and_ordered_lists():
    lines = md("- eins\n- zwei\n\n1. erster\n2. zweiter")
    assert lines[0].startswith("• eins")
    assert lines[3].startswith("1. erster") and lines[4].startswith("2. zweiter")


def test_nested_list_keeps_indent():
    lines = md("- oben\n  - unten")
    assert lines[1].startswith("  • unten")


def test_task_list_uses_checkboxes():
    lines = md("- [ ] offen\n- [x] fertig\n- [X] gross\n- normal")
    assert lines[0] == "- [ ] offen".replace("- [ ]", r.TASK[" "]) and lines[0].startswith("☐")
    assert lines[1].startswith("☑") and lines[2].startswith("☑")
    assert lines[3].startswith("•")  # a plain bullet is untouched
    assert "[ ]" not in "".join(lines) and "[x]" not in "".join(lines).lower()


def test_long_list_item_wraps_under_the_marker():
    lines = md("- ein langer Punkt der ueber die verfuegbare Breite hinauslaeuft")
    assert len(lines) > 1 and lines[0].startswith("• ")
    assert lines[1].startswith("  ") and "•" not in lines[1]


# ---- code blocks
def test_code_block_keeps_spaces_and_gets_code_style():
    out = raw("```python\ndef f(x):\n    return x\n```")
    plain = PLAIN(out)
    assert plain == ["  def f(x):".ljust(40), "      return x".ljust(40)]  # padded to the full width
    assert all("48;5;236" in l for l in out)  # panel background marks the block


def test_code_block_chunks_long_lines():
    lines = PLAIN(raw("```\n" + "x" * 100 + "\n```"))
    assert len(lines) == 3 and all(len(l) <= 40 for l in lines)


def test_code_fence_toggles_and_hides_the_fence():
    lines = md("```\n**nicht fett**\n```\nnach dem block")
    assert PLAIN(raw("```\n**nicht fett**\n```"))[0].strip() == "**nicht fett**"
    assert lines[-1] == "nach dem block"


# ---- quotes / rules
def test_quote_gutter_and_italic():
    out = raw("> zitat")
    assert PLAIN(out) == ["│ zitat"] and ";3" in out[0]


def test_horizontal_rule_fills_width():
    lines = md("---", 20)
    assert lines == ["─" * 20]


def test_blank_lines_collapse():
    assert md("a\n\n\n\nb") == ["a", "", "b"]


# ---- tables
def test_table_renders_header_rule_and_rows():
    t = "| Tool | Ergebnis | Status |\n|---|---|---|\n| delegate | 4 | ok |\n| peer | 6 | ok |"
    lines = md(t, 40)
    assert lines[0] == "Tool      Ergebnis  Status"
    assert lines[1] == "────────  ────────  ──────"  # rule spans each column, 2-space gap
    assert lines[2] == "delegate  4         ok"
    assert lines[3] == "peer      6         ok"
    assert "|" not in "".join(lines)


def test_table_header_is_bold():
    t = "| A | B |\n|---|---|\n| 1 | 2 |"
    out = raw(t, 40)
    assert ";1" in out[0]  # header bold
    assert ";1" not in out[2]  # data row not bold


def test_table_shrinks_to_fit_width():
    t = "| SehrLangerHeader | AuchLang | Kurz |\n|---|---|---|\n| a | b | c |"
    lines = md(t, 20)
    assert all(len(l) <= 20 for l in lines)


def test_single_pipe_line_is_not_a_table():
    assert md("| nur eine Zeile") == ["| nur eine Zeile"]


def test_table_between_text():
    t = "vorher\n| A | B |\n|---|---|\n| 1 | 2 |\nnachher"
    lines = md(t, 40)
    assert lines[0] == "vorher"
    assert lines[-1] == "nachher"
    assert "|" not in "".join(lines)


# ---- view integration
def test_agent_block_is_rendered_as_markdown(tmp_path, monkeypatch):
    monkeypatch.setenv("NECRO_DB", str(tmp_path / "necro.db"))
    import necro

    app = necro.App()
    plain = PLAIN(app.render(necro.Block("agent", "# Titel\n- punkt\n"), 40))
    assert plain[0].strip() == "Titel" and plain[1].startswith("• punkt")


# ---- table cells (Session 66: cells went through no inline() pass, so `code`
# and **bold** leaked their markers into the rendered table)
def test_table_cells_render_inline_markup():
    t = "| Tool | Status |\n|---|---|\n| `bash` | **rot** |"
    lines = md(t, 40)
    assert lines[2] == "bash  rot", lines
    out = raw(t, 40)
    assert ";1;33" not in out[2] and "`" not in PLAIN(out[2]) and "*" not in PLAIN(out[2])
    assert ";1" in out[0]  # header still bold


def test_table_long_cell_is_cut_with_ellipsis():
    """A cell wider than its column must be visibly cut (…), not silently
    truncated - and never wider than the terminal."""
    t = ("| Pfad | Beschreibung |\n|---|---|\n"
         "| /workspace/necro/modules/core/sessiondb.py | "
         "diese Beschreibung ist absichtlich viel zu lang fuer jede Spalte |")
    lines = md(t, 60)
    assert all(len(p) <= 60 for p in lines), [len(p) for p in lines]
    assert any(p.endswith("…") for p in lines[1:]), lines


def test_table_never_exceeds_width():
    """Guard against the shrink path: no rendered table line may be wider than
    the terminal (a wider line wraps and desyncs the incremental draw)."""
    t = ("| A | B | C |\n|---|---|---|\n"
         "| ein sehr langer Zellinhalt mit viel Text | noch mehr Text | dritte Zelle |\n"
         "| kurz | **fett und lang genug** | `code der laenger ist` |")
    for w in (20, 33, 40, 61, 80, 120):
        for line in md(t, w):
            assert len(line) <= w, (w, len(line), line)
