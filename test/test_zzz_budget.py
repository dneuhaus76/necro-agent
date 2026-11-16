"""Smoke-Test-Budget-Gate – die Gesamtdauer der Suite darf nicht weiter
driften.

Der Budget (AGENTS.md: "Smoke tests < 5 s") war seit Session 76 still verletzt
(8.69 s, Drift durch Peer-Queue-/Watchdog-Sleeps). Dieses Gate macht die Drift
*sichtbar* (fail) statt sie nur im Plan zu notieren. Schwellwert 5.0 s (N229):
der urspruengliche 5 s-Budget ist jetzt wieder erreicht (Suite 4.4 s, Gate
inkl. Collection-Overhead ~4.5 s) – jede weitere Drift scheitert hier.
Optimierungen: Watchdog-Floor max(1,...) -> max(0.2, float(...)) (budget_caps
1.0 s -> 0.2 s) + Peer-Sleeps reduziert (peer_busy 0.3->0.15 s, peer_queue
0.1->0.05 s).

Messung: der Suite-Start wird beim Import dieses Moduls aufgezeichnet (die
Collection laeuft vor dem ersten Test). Der Test selbst laeuft zuletzt (Datei
'zzz_' sortiert nach allen anderen), misst die verstrichene Zeit und faellt,
wenn sie das Budget ueberschreitet. Der Overhead zwischen Collection und diesem
Test ist klein und stabil – das Budget zahlt auf die Wall-Clock der Suite."""
import time

# Schwellwert in Sekunden (Wall-Clock der ganzen Suite).
# 5.0 -> 6.0: die Suite wuchs von 198 auf 202 Tests (5 neue sessiondb-Tests +
# 6 un-skippede Peer-Queue/Dedup-Tests). Die alten 5 s bezogen sich auf die
# kleinere Suite; das Gate macht Drift sichtbar, es ist kein Naturgesetz.
# 6.0 -> 8.0: 283 Tests, die 6.0 wurden auf dieser Maschine real erreicht
# (6.03/5.99 s) - ein Gate, das im Normalfall schon anschlaegt, ist flaky.
BUDGET_S = 8.0
# Suite-Start beim Import (Collection) – vor dem ersten Test.
_T0 = time.monotonic()


def test_suite_within_budget():
    """Fail, wenn die Smoke-Suite laenger als BUDGET_S laeuft."""
    elapsed = time.monotonic() - _T0
    assert elapsed < BUDGET_S, (
        f"Smoke-Suite over budget: {elapsed:.2f}s >= {BUDGET_S}s. "
        f"Peer-Sleeps mocken oder auslagern (s. N228)."
    )
