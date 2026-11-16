"""NECRO.md (project instructions, incl. the Peering & Consultation rules) is
loaded and appended to the SYSTEM prompt.

N227 originally tested NECROMANCER.md as a separate file; commit d24e7a2 merged
it into NECRO.md, so the peering rules now live in NECRO.md and this test checks
that NECRO.md's content reaches the SYSTEM prompt (not a Waise: present but not
effective). Optional – if the file is absent in the cwd, nothing to assert."""
import necro


def test_necro_md_loaded_into_system():
    if necro.NECRO_MD_PATH is None:
        # file absent in this cwd -> optional, nothing to assert
        assert necro.NECRO_MD == ""
        return
    assert necro.NECRO_MD  # non-empty content read
    assert necro.NECRO_MD in necro.SYSTEM  # actual content, not just the header
    assert "Peering & Consultation" in necro.SYSTEM  # peering rules present


def test_necro_md_appended_once():
    # the loader appends the file content exactly once (no double-injection)
    if necro.NECRO_MD:
        assert necro.SYSTEM.count(necro.NECRO_MD) == 1


def test_necro_md_precedence_over_agents():
    # NECRO.md and AGENTS.md both exist in this repo; the loader uses if/elif,
    # so NECRO.md wins and AGENTS.md is not appended.
    if necro.NECRO_MD and necro.AGENTS_MD:
        assert necro.NECRO_MD in necro.SYSTEM
        assert necro.AGENTS_MD not in necro.SYSTEM
