"""Gruppens historik: ögonblicksbilder per dag och flödet mellan grupper vecka för vecka."""

from __future__ import annotations

from radar import history
from radar.db import connect


def test_snapshot_overwrites_same_day_and_flow_reports_moves(tmp_path):
    connection = connect(tmp_path / "radar.db")
    with connection:
        connection.execute("INSERT INTO companies(id, name) VALUES (1, 'A'), (2, 'B'), (3, 'C')")
    rows = lambda a, b, c: [{"id": 1, "band": a, "runway_today": 5.0}, {"id": 2, "band": b, "runway_today": 2.0},
                            {"id": 3, "band": c, "runway_today": 20.0}]
    history.snapshot(connection, rows("3_9", "under_3", "over_18"), "2026-11-02")   # måndag vecka 45
    history.snapshot(connection, rows("3_9", "under_3", "over_18"), "2026-11-02T13:00")  # samma dag: skrivs över
    history.snapshot(connection, rows("3_9", "acute", "over_18"), "2026-11-05")
    history.snapshot(connection, rows("under_3", "acute", "raising"), "2026-11-10")  # vecka 46
    assert connection.execute("SELECT COUNT(*) FROM band_history").fetchone()[0] == 9
    data = history.flow(connection, "2026-11-12", weeks=4)
    assert [w["week"] for w in data["weeks"]] == ["2026-11-02", "2026-11-09"]
    first, second = data["weeks"]
    assert first["moves"] == [] and first["counts"]["acute"] == 1 and first["counts"]["3_9"] == 1   # sista bilden i veckan
    assert [(m["name"], m["from"], m["to"]) for m in second["moves"]] == [("A", "3_9", "under_3"), ("C", "over_18", "raising")]
    assert second["counts"] == {**{b: 0 for b in history.BANDS}, "under_3": 1, "acute": 1, "raising": 1}
