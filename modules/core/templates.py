"""pi-style prompt templates for slash commands (/review, /test, /plan).
The templates save ad-hoc prompting and make runs comparable (identical starting
conditions for the self-improvement metrics). Pure function - unit-testable."""
TEMPLATES = {
    "/review": ("Review the following code: correctness, bugs, edge cases, style. "
                "List findings in priority order (bug > simplification > style), each with "
                "file:line and a concrete fix proposal. Do not change anything yet."),
    "/test": ("Write or extend a minimal, parseable test suite for the following code "
              "(unit tests for pure logic, no server). Run the tests and report pass/fail. "
              "Focus: "),
    "/plan": ("Analyse the following task and create a concrete plan: steps, affected files, "
              "risks, and verification (how each step is tested). Do not implement yet."),
}
def expand(cmd, rest=""):
    """'/review modules/tools.py' -> template + focus text; None when cmd is unknown."""
    t = TEMPLATES.get(cmd)
    if t is None:
        return None
    return (t.rstrip() + " " + rest).strip() if rest else t
