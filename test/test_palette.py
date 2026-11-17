"""Palette: 256-color indices, the self-documenting role table, cfg validation."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import necro
from modules.core import palette as p
from modules.core.config import validate

def test_is_index_accepts_the_whole_range():
    assert p.is_index(0) and p.is_index(255) and p.is_index(216)
    assert p.is_index("216") and p.is_index(" 0 ")

def test_is_index_rejects_bad_values():
    for bad in (256, -1, True, False, "nope", "", 1.5, None, [], {}, "#ff0000"):
        assert not p.is_index(bad), bad

def test_resolve_applies_overrides_and_ignores_junk():
    eff = p.resolve({"accent": 208, "user": "216", "nope": 5, "error": 999, "text": True})
    assert eff["accent"] == 208 and eff["user"] == 216
    assert eff["error"] == p.PALETTE["error"]   # out of range keeps the default
    assert eff["text"] == p.PALETTE["text"]     # bool is not an index
    assert "nope" not in eff
    assert p.resolve(None) == p.PALETTE

def test_every_role_is_documented():
    """The help table needs one line per role - an undocumented role is not usable."""
    assert set(p.ROLE_DOC) == set(p.PALETTE)
    assert all(p.ROLE_DOC[r].strip() for r in p.PALETTE)
    assert all(isinstance(p.PALETTE[r], int) and 0 <= p.PALETTE[r] <= 255 for r in p.PALETTE)

def test_table_lists_every_role_with_index_and_purpose():
    text = p.table()
    lines = text.splitlines()
    assert len(lines) == len(p.PALETTE)
    for role, idx in p.PALETTE.items():
        line = next(l for l in lines if l.split()[0] == role)
        assert str(idx) in line and p.ROLE_DOC[role] in line

def test_colors_flag_prints_the_table(capsys, monkeypatch):
    """necro.py --colors is the user-facing help for cfg 'colors'."""
    monkeypatch.setattr(sys, "argv", ["necro.py", "--colors"])
    necro.main()
    out = capsys.readouterr().out
    assert "cfg 'colors'" in out
    for role in p.PALETTE:
        assert role in out

def test_cfg_validation_accepts_indices_and_rejects_the_rest():
    assert validate({"colors": {"accent": 208, "user": "216", "_note": "comment"}}) == []
    assert validate({"colors": {"accent": 999}})
    assert validate({"colors": {"accent": "#ff8700"}})
    assert validate({"colors": {"unknown_role": 1}})

def test_project_cfg_documents_the_palette():
    """necro.cfg ships the role table as a comment, so the help is where it is needed."""
    cfg = json.loads(Path(necro.__file__).with_name("necro.cfg").read_text())
    doc = cfg.get("_doc_colors")
    assert isinstance(doc, list)
    for role, idx in p.PALETTE.items():
        assert any(l.startswith(f"{role} {idx} - ") for l in doc), role
    assert validate(cfg) == []

def test_palette_module_main_prints_the_table():
    out = subprocess.run([sys.executable, "-m", "modules.core.palette"],
                         cwd=Path(necro.__file__).parent, capture_output=True, text=True)
    assert out.returncode == 0
    assert "user" in out.stdout and "216" in out.stdout
