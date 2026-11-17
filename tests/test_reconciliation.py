"""End-to-end self tests for the interval reconciliation API."""
from __future__ import annotations

import os
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture()
def ctx():
    tmp = tempfile.TemporaryDirectory()
    db_path = os.path.join(tmp.name, "test.db")
    app = create_app(Settings(db_path=db_path, fail_device=None))
    with TestClient(app) as client:
        yield client, db_path, tmp
    tmp.cleanup()


def reading(source, device, ts, meter, kwh, rev=1):
    return {
        "source_id": source, "revision": rev, "device_id": device,
        "ts": ts, "meter_no": meter, "cumulative_kwh": kwh,
    }


def handover(source, device, ts, old_m, old_v, new_m, new_v, rev=1):
    return {
        "source_id": source, "revision": rev, "device_id": device, "ts": ts,
        "old_meter_no": old_m, "old_final_kwh": old_v,
        "new_meter_no": new_m, "new_initial_kwh": new_v,
    }


def reconcile(client, total, subs, start="2026-01-01T00:00:00Z",
              end="2026-01-01T02:00:00Z", tol=5.0):
    return client.post(
        "/reconciliations",
        json={
            "total_device_id": total,
            "sub_device_ids": subs,
            "start_ts": start,
            "end_ts": end,
            "tolerance_kwh": tol,
        },
    )


def seed_three_devices(client):
    """Total T plus subs A/B, all sharing boundaries 00, 01, 02."""
    series = {
        "T": [("T0", "2026-01-01T00:00:00Z", 1000.0),
              ("T1", "2026-01-01T01:00:00Z", 1100.0),
              ("T2", "2026-01-01T02:00:00Z", 1250.0)],
        "A": [("A0", "2026-01-01T00:00:00Z", 100.0),
              ("A1", "2026-01-01T01:00:00Z", 150.0),
              ("A2", "2026-01-01T02:00:00Z", 200.0)],
        "B": [("B0", "2026-01-01T00:00:00Z", 50.0),
              ("B1", "2026-01-01T01:00:00Z", 70.0),
              ("B2", "2026-01-01T02:00:00Z", 90.0)],
    }
    for dev, rows in series.items():
        for src, ts, kwh in rows:
            r = client.post("/readings", json=reading(src, dev, ts, f"M-{dev}", kwh))
            assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# Published conclusions
# ---------------------------------------------------------------------------

def test_publishes_per_segment_differences_and_provenance(ctx):
    client, db_path, _ = ctx
    seed_three_devices(client)

    r = reconcile(client, "T", ["A", "B"], tol=50.0)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "published"
    assert body["version"] == 1
    assert body["exceeded"] is True
    assert body["blocked_devices"] == []
    assert [s["start_ts"] for s in body["segments"]] == [
        "2026-01-01T00:00:00.000Z", "2026-01-01T01:00:00.000Z",
    ]
    seg1, seg2 = body["segments"]
    # segment 1: total 100, subs (150-100)+(70-50)=70, diff 30 (<= 50)
    assert seg1["total_kwh"] == pytest.approx(100.0)
    assert seg1["sub_kwh"] == pytest.approx(70.0)
    assert seg1["difference_kwh"] == pytest.approx(30.0)
    assert seg1["exceeded"] is False
    # segment 2: total 150, subs 50+20=70, diff 80 (> 50)
    assert seg2["difference_kwh"] == pytest.approx(80.0)
    assert seg2["exceeded"] is True

    # Readings and source provenance are reported for both sides.
    assert seg1["total_readings"][0]["source_id"] == "T0"
    assert seg1["total_readings"][1]["source_id"] == "T1"
    assert {r["device_id"] for r in seg1["sub_readings"]} == {"A", "B"}
    assert {(r["source_id"]) for r in seg1["sub_readings"]} == {"A0", "A1", "B0", "B1"}
    assert seg1["total_handover"] is None and seg1["sub_handovers"] == []

    # Conclusion is persisted and queryable.
    got = client.get(f"/reconciliations/{body['id']}").json()
    assert got["status"] == "published" and got["segments"] == body["segments"]

    # latest lookup by the same parameters returns it; sub order is irrelevant.
    latest = client.get(
        "/reconciliations/latest",
        params={
            "total_device_id": "T", "sub_device_ids": "B,A",
            "start_ts": "2026-01-01T00:00:00Z", "end_ts": "2026-01-01T02:00:00Z",
            "tolerance_kwh": 50.0,
        },
    )
    assert latest.status_code == 200
    assert latest.json()["id"] == body["id"]

    listed = client.get("/reconciliations", params={"total_device_id": "T"}).json()
    assert [i["id"] for i in listed["items"]] == [body["id"]]


def test_tolerance_boundary_is_inclusive(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("T0", "T", "2026-01-01T00:00:00Z", "M", 100.0))
    client.post("/readings", json=reading("T1", "T", "2026-01-01T01:00:00Z", "M", 120.0))
    client.post("/readings", json=reading("S0", "S", "2026-01-01T00:00:00Z", "N", 10.0))
    client.post("/readings", json=reading("S1", "S", "2026-01-01T01:00:00Z", "N", 30.0))
    # total 20, sub 20, difference 0
    body = reconcile(
        client, "T", ["S"], start="2026-01-01T00:00:00Z",
        end="2026-01-01T01:00:00Z", tol=0.0,
    ).json()
    assert body["status"] == "published" and body["exceeded"] is False

    # Second segment: total +3, sub +5, difference -2.
    client.post("/readings", json=reading("S2", "S", "2026-01-01T02:00:00Z", "N", 35.0))
    client.post("/readings", json=reading("T2", "T", "2026-01-01T02:00:00Z", "M", 123.0))

    # Exactly at the tolerance is not exceeded (boundary inclusive).
    body = reconcile(
        client, "T", ["S"], start="2026-01-01T01:00:00Z",
        end="2026-01-01T02:00:00Z", tol=2.0,
    ).json()
    assert body["segments"][0]["difference_kwh"] == pytest.approx(-2.0)
    assert body["exceeded"] is False

    # Beyond the tolerance is exceeded.
    body = reconcile(
        client, "T", ["S"], start="2026-01-01T01:00:00Z",
        end="2026-01-01T02:00:00Z", tol=1.5,
    ).json()["segments"][0]
    assert body["difference_kwh"] == pytest.approx(-2.0) and body["exceeded"] is True

    # Full window: two segments (0 and -2); any exceeded segment makes the
    # whole conclusion exceeded.
    body = reconcile(
        client, "T", ["S"], start="2026-01-01T00:00:00Z",
        end="2026-01-01T02:00:00Z", tol=1.0,
    ).json()
    assert [s["difference_kwh"] for s in body["segments"]] == pytest.approx([0.0, -2.0])
    assert body["exceeded"] is True


def test_handover_provenance_reported_for_cross_meter_sub(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("T0", "T", "2026-01-01T00:00:00Z", "TM", 100.0))
    client.post("/readings", json=reading("T1", "T", "2026-01-01T02:00:00Z", "TM", 180.0))
    client.post("/readings", json=reading("S0", "S", "2026-01-01T00:00:00Z", "SM-old", 100.0))
    client.post("/readings", json=reading("S1", "S", "2026-01-01T02:00:00Z", "SM-new", 15.0))
    client.post("/handovers", json=handover(
        "H1", "S", "2026-01-01T01:00:00Z", "SM-old", 140.0, "SM-new", 5.0))

    body = reconcile(client, "T", ["S"]).json()
    assert body["status"] == "published"
    seg = body["segments"][0]
    assert seg["total_kwh"] == pytest.approx(80.0)
    # (140-100)+(15-5)=50
    assert seg["sub_kwh"] == pytest.approx(50.0)
    assert seg["difference_kwh"] == pytest.approx(30.0)
    assert seg["sub_handovers"] == [{
        "source_id": "H1", "revision": 1, "device_id": "S",
        "ts": "2026-01-01T01:00:00.000Z",
        "old_meter_no": "SM-old", "old_final_kwh": 140.0,
        "new_meter_no": "SM-new", "new_initial_kwh": 5.0,
    }]


# ---------------------------------------------------------------------------
# Blocked conclusions: nothing partial is published
# ---------------------------------------------------------------------------

def test_missing_coverage_blocks_without_partial_segments(ctx):
    client, _, _ = ctx
    # A covers 00..02, B only starts at 00:30 -> no reading at/before start.
    client.post("/readings", json=reading("T0", "T", "2026-01-01T00:00:00Z", "M", 100.0))
    client.post("/readings", json=reading("T1", "T", "2026-01-01T02:00:00Z", "M", 200.0))
    client.post("/readings", json=reading("A0", "A", "2026-01-01T00:00:00Z", "M", 10.0))
    client.post("/readings", json=reading("A1", "A", "2026-01-01T02:00:00Z", "M", 60.0))
    client.post("/readings", json=reading("B0", "B", "2026-01-01T00:30:00Z", "M", 5.0))
    client.post("/readings", json=reading("B1", "B", "2026-01-01T02:00:00Z", "M", 25.0))

    body = reconcile(client, "T", ["A", "B"]).json()
    assert body["status"] == "blocked"
    assert body["segments"] is None
    assert body["exceeded"] is None
    assert body["blocked_devices"] == ["B"]
    assert "B" in body["detail"] and "incomplete_coverage" in body["detail"]


def test_missing_end_coverage_blocks_whole_conclusion(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("T0", "T", "2026-01-01T00:00:00Z", "M", 100.0))
    client.post("/readings", json=reading("T1", "T", "2026-01-01T02:00:00Z", "M", 200.0))
    client.post("/readings", json=reading("A0", "A", "2026-01-01T00:00:00Z", "M", 10.0))
    # A has no reading at or after the window end.
    body = reconcile(client, "T", ["A"]).json()
    assert body["status"] == "blocked" and body["blocked_devices"] == ["A"]
    assert body["segments"] is None


def test_undeterminable_energy_blocks_and_names_device(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("T0", "T", "2026-01-01T00:00:00Z", "M", 100.0))
    client.post("/readings", json=reading("T1", "T", "2026-01-01T02:00:00Z", "M", 200.0))
    # S changes meter without a handover -> energy undeterminable.
    client.post("/readings", json=reading("S0", "S", "2026-01-01T00:00:00Z", "M-old", 10.0))
    client.post("/readings", json=reading("S1", "S", "2026-01-01T02:00:00Z", "M-new", 20.0))

    body = reconcile(client, "T", ["S"]).json()
    assert body["status"] == "blocked"
    assert body["blocked_devices"] == ["S"]
    assert "undeterminable" in body["detail"] and "handover_missing" in body["detail"]
    assert body["segments"] is None


def test_boundary_mismatch_blocks(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("T0", "T", "2026-01-01T00:00:00Z", "M", 100.0))
    client.post("/readings", json=reading("T1", "T", "2026-01-01T02:00:00Z", "M", 200.0))
    client.post("/readings", json=reading("S0", "S", "2026-01-01T00:00:00Z", "M", 10.0))
    client.post("/readings", json=reading("S9", "S", "2026-01-01T01:00:00Z", "M", 40.0))
    client.post("/readings", json=reading("S1", "S", "2026-01-01T02:00:00Z", "M", 70.0))

    body = reconcile(client, "T", ["S"]).json()
    assert body["status"] == "blocked"
    assert body["blocked_devices"] == ["S"]
    assert "boundary_mismatch" in body["detail"]
    assert body["segments"] is None


def test_pending_rebuild_blocks(ctx):
    client, _, _ = ctx
    seed_three_devices(client)
    # A late reading for A, with the automatic rebuild forced to fail.
    r = client.post(
        "/readings",
        headers={"X-Fail-Rebuild": "true"},
        json=reading("A1b", "A", "2026-01-01T01:30:00Z", "M-A", 175.0),
    )
    assert r.status_code == 201
    assert r.json()["rebuild"]["success"] is False
    assert client.get("/intervals", params={"device_id": "A"}).json() == []

    body = reconcile(client, "T", ["A", "B"]).json()
    assert body["status"] == "blocked"
    assert body["blocked_devices"] == ["A"]
    assert "pending_rebuild" in body["detail"]
    assert body["segments"] is None


def test_blocked_conclusion_is_persisted_and_queryable(ctx):
    client, _, _ = ctx
    body = reconcile(client, "T", ["A"]).json()
    assert body["status"] == "blocked"
    stored = client.get(f"/reconciliations/{body['id']}").json()
    assert stored["status"] == "blocked" and stored["blocked_devices"] == ["T", "A"]
    listed = client.get(
        "/reconciliations", params={"status": "blocked"}
    ).json()["items"]
    assert [i["id"] for i in listed] == [body["id"]]


# ---------------------------------------------------------------------------
# Corrections revoke conclusions; only a new reconciliation republishes
# ---------------------------------------------------------------------------

def test_reading_correction_supersedes_and_reconciliation_republishes(ctx):
    client, _, _ = ctx
    seed_three_devices(client)
    first = reconcile(client, "T", ["A", "B"], tol=50.0).json()
    assert first["status"] == "published"

    # Correct sub A's 02:00 reading (revision 2) -> old conclusion revoked.
    r = client.post(
        "/readings",
        json=reading("A2", "A", "2026-01-01T02:00:00Z", "M-A", 210.0, rev=2),
    )
    assert r.status_code == 201

    # It is no longer current and not returned by latest.
    latest = client.get(
        "/reconciliations/latest",
        params={
            "total_device_id": "T", "sub_device_ids": "A,B",
            "start_ts": "2026-01-01T00:00:00Z", "end_ts": "2026-01-01T02:00:00Z",
            "tolerance_kwh": 50.0,
        },
    )
    assert latest.status_code == 404
    # But it stays traceable with its superseded status and reason.
    old = client.get(f"/reconciliations/{first['id']}").json()
    assert old["status"] == "superseded"
    assert "A2" in old["superseded_reason"] and "revision 2" in old["superseded_reason"]
    assert old["segments"] == first["segments"]
    assert old["superseded_at"] is not None

    # Re-reconciling produces a new version based on the corrected data.
    second = reconcile(client, "T", ["A", "B"], tol=50.0).json()
    assert second["status"] == "published" and second["version"] == 2
    assert second["id"] != first["id"]
    # segment 2: subs (50 + 20) = 70 -> total 150 - 70 = 80? corrected A2=210,
    # so sub A segment 2 is 60, sub B is 20, total diff = 70.
    assert second["segments"][1]["sub_kwh"] == pytest.approx(80.0)
    assert second["segments"][1]["difference_kwh"] == pytest.approx(70.0)

    # History keeps both versions; current view only shows the new one.
    all_rows = client.get(
        "/reconciliations", params={"status": "all", "total_device_id": "T"}
    ).json()["items"]
    assert {row["id"] for row in all_rows} == {first["id"], second["id"]}
    current_rows = client.get(
        "/reconciliations", params={"total_device_id": "T"}
    ).json()["items"]
    assert [row["id"] for row in current_rows] == [second["id"]]


def test_handover_correction_supersedes(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("T0", "T", "2026-01-01T00:00:00Z", "TM", 100.0))
    client.post("/readings", json=reading("T1", "T", "2026-01-01T02:00:00Z", "TM", 180.0))
    client.post("/readings", json=reading("S0", "S", "2026-01-01T00:00:00Z", "SM-old", 100.0))
    client.post("/readings", json=reading("S1", "S", "2026-01-01T02:00:00Z", "SM-new", 15.0))
    client.post("/handovers", json=handover(
        "H1", "S", "2026-01-01T01:00:00Z", "SM-old", 140.0, "SM-new", 5.0))
    first = reconcile(client, "T", ["S"]).json()
    assert first["status"] == "published"

    client.post("/handovers", json=handover(
        "H1", "S", "2026-01-01T01:00:00Z", "SM-old", 150.0, "SM-new", 5.0, rev=2))
    assert client.get(f"/reconciliations/{first['id']}").json()["status"] == "superseded"

    second = reconcile(client, "T", ["S"]).json()
    assert second["status"] == "published" and second["version"] == 2
    assert second["segments"][0]["sub_kwh"] == pytest.approx(60.0)
    assert second["segments"][0]["sub_handovers"][0]["revision"] == 2


def test_idempotent_resubmission_does_not_supersede(ctx):
    client, _, _ = ctx
    seed_three_devices(client)
    first = reconcile(client, "T", ["A", "B"], tol=50.0).json()
    # Exact same reading again -> idempotent, no accepted correction.
    r = client.post("/readings", json=reading("A2", "A", "2026-01-01T02:00:00Z", "M-A", 200.0))
    assert r.status_code == 201 and r.json()["idempotent"] is True
    assert client.get(f"/reconciliations/{first['id']}").json()["status"] == "published"


def test_failed_rebuild_does_not_resurrect_old_conclusion(ctx):
    client, _, _ = ctx
    seed_three_devices(client)
    first = reconcile(client, "T", ["A", "B"], tol=50.0).json()
    assert first["status"] == "published"

    # Correction accepted, its rebuild fails: conclusion stays revoked.
    r = client.post(
        "/readings",
        headers={"X-Fail-Rebuild": "true"},
        json=reading("A1", "A", "2026-01-01T01:00:00Z", "M-A", 155.0, rev=2),
    )
    assert r.status_code == 201 and r.json()["rebuild"]["success"] is False
    assert client.get(f"/reconciliations/{first['id']}").json()["status"] == "superseded"
    assert client.get(
        "/reconciliations", params={"status": "current", "total_device_id": "T"}
    ).json()["items"] == []

    # Retry the rebuild: data comes back current, but the old conclusion does not.
    assert client.post("/rebuild", params={"device_id": "A"}).json()["success"] is True
    assert client.get(f"/reconciliations/{first['id']}").json()["status"] == "superseded"
    assert client.get(
        "/reconciliations", params={"status": "current", "total_device_id": "T"}
    ).json()["items"] == []

    # Only a fresh reconciliation publishes again.
    second = reconcile(client, "T", ["A", "B"], tol=50.0).json()
    assert second["status"] == "published" and second["version"] == 2


def test_unrelated_device_conclusion_stays_current(ctx):
    client, _, _ = ctx
    seed_three_devices(client)
    # Second independent pair.
    client.post("/readings", json=reading("U0", "U", "2026-01-01T00:00:00Z", "M-U", 500.0))
    client.post("/readings", json=reading("U1", "U", "2026-01-01T02:00:00Z", "M-U", 600.0))
    client.post("/readings", json=reading("V0", "V", "2026-01-01T00:00:00Z", "M-V", 10.0))
    client.post("/readings", json=reading("V1", "V", "2026-01-01T02:00:00Z", "M-V", 90.0))

    pair1 = reconcile(client, "T", ["A", "B"]).json()
    pair2 = reconcile(client, "U", ["V"]).json()
    assert pair1["status"] == pair2["status"] == "published"

    # Correct a sub of pair1 only.
    client.post("/readings", json=reading("A1", "A", "2026-01-01T01:00:00Z", "M-A", 160.0, rev=2))
    assert client.get(f"/reconciliations/{pair1['id']}").json()["status"] == "superseded"
    assert client.get(f"/reconciliations/{pair2['id']}").json()["status"] == "published"
    assert client.get(
        "/reconciliations", params={"total_device_id": "U"}
    ).json()["items"][0]["id"] == pair2["id"]


def test_repeated_reconciliation_without_correction_keeps_history(ctx):
    client, _, _ = ctx
    seed_three_devices(client)
    first = reconcile(client, "T", ["A", "B"]).json()
    second = reconcile(client, "T", ["A", "B"]).json()
    assert second["version"] == 2
    assert client.get(f"/reconciliations/{first['id']}").json()["status"] == "superseded"
    current = client.get(f"/reconciliations/{second['id']}").json()
    assert current["status"] == "published"


def test_new_conclusion_uses_committed_data_after_correction(ctx):
    client, _, _ = ctx
    seed_three_devices(client)
    reconcile(client, "T", ["A", "B"], tol=50.0)

    # Correction accepted and rebuilt...
    client.post("/readings", json=reading("A2", "A", "2026-01-01T02:00:00Z", "M-A", 260.0, rev=2))
    # ...then the interleaved reconciliation reads the corrected, committed
    # snapshot only (no mix of old stale rows).
    body = reconcile(client, "T", ["A", "B"], tol=50.0).json()
    assert body["status"] == "published"
    seg2 = body["segments"][1]
    assert seg2["sub_kwh"] == pytest.approx(110.0 + 20.0)
    assert seg2["difference_kwh"] == pytest.approx(20.0)
    a_readings = [r for r in seg2["sub_readings"] if r["device_id"] == "A"]
    assert [r["revision"] for r in a_readings] == [1, 2]
    assert a_readings[1]["cumulative_kwh"] == 260.0


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_reconciliation_validation(ctx):
    client, _, _ = ctx
    # Empty sub list, blank device, total also listed as sub, bad window.
    r = client.post("/reconciliations", json={
        "total_device_id": "T", "sub_device_ids": [], "start_ts": "2026-01-01T00:00:00Z",
        "end_ts": "2026-01-01T02:00:00Z", "tolerance_kwh": 1.0})
    assert r.status_code == 422
    r = client.post("/reconciliations", json={
        "total_device_id": "T", "sub_device_ids": [" "], "start_ts": "2026-01-01T00:00:00Z",
        "end_ts": "2026-01-01T02:00:00Z", "tolerance_kwh": 1.0})
    assert r.status_code == 422
    r = client.post("/reconciliations", json={
        "total_device_id": "T", "sub_device_ids": ["T"], "start_ts": "2026-01-01T00:00:00Z",
        "end_ts": "2026-01-01T02:00:00Z", "tolerance_kwh": 1.0})
    assert r.status_code == 422
    r = client.post("/reconciliations", json={
        "total_device_id": "T", "sub_device_ids": ["A"], "start_ts": "2026-01-01T02:00:00Z",
        "end_ts": "2026-01-01T02:00:00Z", "tolerance_kwh": 1.0})
    assert r.status_code == 422
    r = client.post("/reconciliations", json={
        "total_device_id": "T", "sub_device_ids": ["A"], "start_ts": "2026-01-01T00:00:00Z",
        "end_ts": "2026-01-01T02:00:00Z", "tolerance_kwh": -1.0})
    assert r.status_code == 422
    # Naive timestamps rejected.
    r = client.post("/reconciliations", json={
        "total_device_id": "T", "sub_device_ids": ["A"], "start_ts": "2026-01-01T00:00:00",
        "end_ts": "2026-01-01T02:00:00", "tolerance_kwh": 1.0})
    assert r.status_code == 422


def test_duplicate_sub_devices_are_deduplicated(ctx):
    client, _, _ = ctx
    seed_three_devices(client)
    body = reconcile(client, "T", ["A", "A", "B"], tol=50.0).json()
    assert body["status"] == "published"
    assert body["sub_device_ids"] == ["A", "B"]
    assert body["segments"][0]["sub_kwh"] == pytest.approx(70.0)


def test_unknown_reconciliation_id_returns_404(ctx):
    client, _, _ = ctx
    assert client.get("/reconciliations/99999").status_code == 404
    assert client.get("/reconciliations/latest", params={
        "total_device_id": "T", "sub_device_ids": "A",
        "start_ts": "2026-01-01T00:00:00Z", "end_ts": "2026-01-01T02:00:00Z",
        "tolerance_kwh": 1.0,
    }).status_code == 404


def test_openapi_exposes_reconciliation_endpoints(ctx):
    client, _, _ = ctx
    paths = client.get("/openapi.json").json()["paths"]
    assert "/reconciliations" in paths
    assert "/reconciliations/{recon_id}" in paths
    assert "/reconciliations/latest" in paths
    assert client.get("/docs").status_code == 200


def test_sub_device_filter_and_blocked_details(ctx):
    client, _, _ = ctx
    seed_three_devices(client)
    pair = reconcile(client, "T", ["A", "B"]).json()
    other = reconcile(
        client, "T", ["A"], start="2026-01-01T01:00:00Z", end="2026-01-01T02:00:00Z"
    ).json()
    assert other["status"] == "published"

    only_a = client.get(
        "/reconciliations", params={"sub_device_id": "A"}
    ).json()["items"]
    assert {row["id"] for row in only_a} == {pair["id"], other["id"]}
    # Filtering by the total device only matches the 'total' role.
    only_total = client.get(
        "/reconciliations", params={"sub_device_id": "T"}
    ).json()["items"]
    assert only_total == []

    # Blocked conclusion carries per-device segment details: a backwards B
    # reading makes its segment undeterminable.
    client.post("/readings", json=reading("B1", "B", "2026-01-01T01:00:00Z", "M-B", 40.0, rev=2))
    blocked = reconcile(client, "T", ["A", "B"], tol=100.0).json()
    assert blocked["status"] == "blocked"
    assert blocked["blocked_devices"] == ["B"]
    detail = blocked["blocked_details"][0]
    assert detail["device_id"] == "B"


def test_concurrent_correction_and_reconciliation_uses_one_snapshot(ctx):
    """Interleaved corrections and reconciliations never mix data revisions.

    Total T is fixed at 100 kWh per segment.  Sub A's end reading is corrected
    repeatedly (revision 2/3/4).  Every published conclusion must pair a single
    revision with matching arithmetic; old revisions must end up superseded.
    """
    client, db_path, _ = ctx
    import threading

    client.post("/readings", json=reading("T0", "T", "2026-01-01T00:00:00Z", "M", 100.0))
    client.post("/readings", json=reading("T1", "T", "2026-01-01T01:00:00Z", "M", 100.0))
    client.post("/readings", json=reading("T2", "T", "2026-01-01T02:00:00Z", "M", 100.0))
    client.post("/readings", json=reading("A0", "A", "2026-01-01T00:00:00Z", "M", 100.0))
    client.post("/readings", json=reading("A1", "A", "2026-01-01T01:00:00Z", "M", 100.0))
    client.post("/readings", json=reading("A2", "A", "2026-01-01T02:00:00Z", "M", 100.0))
    client.post("/readings", json=reading("B0", "B", "2026-01-01T00:00:00Z", "M", 10.0))
    client.post("/readings", json=reading("B1", "B", "2026-01-01T01:00:00Z", "M", 20.0))
    client.post("/readings", json=reading("B2", "B", "2026-01-01T02:00:00Z", "M", 30.0))

    published_ids = []
    errors = []

    def corrections():
        try:
            # Linear corrections: revision r has cumulative 100 + 50*(r-1).
            for rev, value in [(2, 150.0), (3, 200.0), (4, 250.0)]:
                r = client.post("/readings", json=reading(
                    "A2", "A", "2026-01-01T02:00:00Z", "M", value, rev=rev))
                assert r.status_code == 201, r.text
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def reconciliations():
        try:
            for _ in range(6):
                r = reconcile(client, "T", ["A", "B"], tol=1000.0)
                assert r.status_code == 201, r.text
                body = r.json()
                if body["status"] == "published":
                    published_ids.append(body["id"])
                    seg2 = body["segments"][1]
                    a_ends = [
                        x for x in seg2["sub_readings"]
                        if x["device_id"] == "A" and x["ts"].startswith("2026-01-01T02")
                    ]
                    assert len(a_ends) == 1
                    rev = a_ends[0]["revision"]
                    assert a_ends[0]["cumulative_kwh"] == 100.0 + 50.0 * (rev - 1)
                    # The total meter is flat (0 kWh); sub B contributes 10.
                    assert seg2["sub_kwh"] == pytest.approx(10.0 + 50.0 * (rev - 1))
                    assert seg2["difference_kwh"] == pytest.approx(-(10.0 + 50.0 * (rev - 1)))
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    t1 = threading.Thread(target=corrections)
    t2 = threading.Thread(target=reconciliations)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert not errors, errors
    assert published_ids

    # Final state: the newest conclusion reflects revision 4; any earlier
    # published conclusion was revoked by the accepted corrections.
    final = reconcile(client, "T", ["A", "B"], tol=1000.0).json()
    assert final["status"] == "published"
    assert final["segments"][1]["sub_kwh"] == pytest.approx(160.0)
    for rid in published_ids:
        row = client.get(f"/reconciliations/{rid}").json()
        if rid != final["id"]:
            assert row["status"] == "superseded"
    current = client.get(
        "/reconciliations", params={"total_device_id": "T", "status": "current"}
    ).json()["items"]
    assert [row["id"] for row in current] == [final["id"]]


def test_total_meter_handover_provenance(ctx):
    client, _, _ = ctx
    # Total meter itself changes meter mid-window.
    client.post("/readings", json=reading("T0", "T", "2026-01-01T00:00:00Z", "TM-old", 100.0))
    client.post("/readings", json=reading("T1", "T", "2026-01-01T02:00:00Z", "TM-new", 30.0))
    client.post("/handovers", json=handover(
        "TH1", "T", "2026-01-01T01:00:00Z", "TM-old", 150.0, "TM-new", 10.0))
    client.post("/readings", json=reading("S0", "S", "2026-01-01T00:00:00Z", "SM", 10.0))
    client.post("/readings", json=reading("S1", "S", "2026-01-01T02:00:00Z", "SM", 80.0))

    body = reconcile(client, "T", ["S"]).json()
    assert body["status"] == "published"
    seg = body["segments"][0]
    # total (150-100)+(30-10) = 70 ; sub 70 ; difference 0
    assert seg["total_kwh"] == pytest.approx(70.0)
    assert seg["difference_kwh"] == pytest.approx(0.0)
    assert seg["total_handover"]["source_id"] == "TH1"
    assert seg["total_handover"]["revision"] == 1
    assert seg["sub_handovers"] == []


def test_same_meter_segment_does_not_report_unused_handover(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("T0", "T", "2026-01-01T00:00:00Z", "TM", 100.0))
    client.post("/readings", json=reading("T1", "T", "2026-01-01T02:00:00Z", "TM", 200.0))
    client.post("/readings", json=reading("S0", "S", "2026-01-01T00:00:00Z", "SM", 10.0))
    client.post("/readings", json=reading("S1", "S", "2026-01-01T02:00:00Z", "SM2", 50.0))
    # First handover SM->SM2 settles; a second, unrelated meter pair overlaps.
    client.post("/handovers", json=handover(
        "H1", "S", "2026-01-01T01:00:00Z", "SM", 40.0, "SM2", 5.0))
    client.post("/handovers", json=handover(
        "H2", "S", "2026-01-01T01:20:00Z", "OTHER", 1.0, "ELSE", 0.0))

    body = reconcile(client, "T", ["S"]).json()
    assert body["status"] == "published"
    assert [h["source_id"] for h in body["segments"][0]["sub_handovers"]] == ["H1"]


def test_ambiguous_handover_blocks(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("T0", "T", "2026-01-01T00:00:00Z", "TM", 100.0))
    client.post("/readings", json=reading("T1", "T", "2026-01-01T02:00:00Z", "TM", 200.0))
    client.post("/readings", json=reading("S0", "S", "2026-01-01T00:00:00Z", "SM", 10.0))
    client.post("/readings", json=reading("S1", "S", "2026-01-01T02:00:00Z", "SM2", 50.0))
    for src, ts in [("H1", "2026-01-01T00:30:00Z"), ("H2", "2026-01-01T01:30:00Z")]:
        client.post("/handovers", json=handover(src, "S", ts, "SM", 40.0, "SM2", 5.0))

    body = reconcile(client, "T", ["S"]).json()
    assert body["status"] == "blocked" and body["blocked_devices"] == ["S"]
    assert "ambiguous_handover" in body["detail"]


def test_boundary_reading_exactly_at_window_edges_included(ctx):
    client, _, _ = ctx
    # Readings exactly at start/end define the boundary; one interior reading
    # on both devices keeps three segments.
    for dev, vals in [("T", [100, 110, 140]), ("S", [10, 20, 50])]:
        for i, v in enumerate(vals):
            client.post("/readings", json=reading(
                f"{dev}{i}", dev, f"2026-01-01T0{i}:00:00Z", f"M-{dev}", v))
    body = reconcile(
        client, "T", ["S"], start="2026-01-01T00:00:00Z", end="2026-01-01T02:00:00Z"
    ).json()
    assert body["status"] == "published"
    assert [s["start_ts"] for s in body["segments"]] == [
        "2026-01-01T00:00:00.000Z", "2026-01-01T01:00:00.000Z",
    ]
    assert [s["difference_kwh"] for s in body["segments"]] == pytest.approx([0.0, 0.0])


def test_duplicate_boundary_timestamp_blocks_as_ambiguous(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("T0", "T", "2026-01-01T00:00:00Z", "M", 100.0))
    client.post("/readings", json=reading("T1", "T", "2026-01-01T02:00:00Z", "M", 200.0))
    # Two authoritative readings from different sources share a timestamp.
    client.post("/readings", json=reading("S0", "S", "2026-01-01T00:00:00Z", "M", 10.0))
    client.post("/readings", json=reading("S0b", "S", "2026-01-01T00:00:00Z", "M", 11.0))
    client.post("/readings", json=reading("S1", "S", "2026-01-01T02:00:00Z", "M", 50.0))

    body = reconcile(client, "T", ["S"]).json()
    assert body["status"] == "blocked" and body["blocked_devices"] == ["S"]
    assert "ambiguous_boundary" in body["detail"]


def test_window_inside_intervals_reports_full_interval_boundaries(ctx):
    client, _, _ = ctx
    # Readings only at 00 and 02;窗口 00:30..01:30 落在区间内部。
    for dev in ["T", "S"]:
        client.post("/readings", json=reading(f"{dev}0", dev, "2026-01-01T00:00:00Z", "M", 100.0))
        client.post("/readings", json=reading(f"{dev}2", dev, "2026-01-01T02:00:00Z", "M", 160.0))
    body = reconcile(
        client, "T", ["S"], start="2026-01-01T00:30:00Z", end="2026-01-01T01:30:00Z"
    ).json()
    assert body["status"] == "published"
    # The segments are the devices' natural interval boundaries (not prorated):
    # the complete covering interval is reported.
    assert len(body["segments"]) == 1
    seg = body["segments"][0]
    assert seg["start_ts"] == "2026-01-01T00:00:00.000Z"
    assert seg["end_ts"] == "2026-01-01T02:00:00.000Z"
    assert seg["difference_kwh"] == pytest.approx(0.0)
