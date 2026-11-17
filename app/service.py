"""Domain logic: revision rules, interval settlement, rebuild transactions.

Key guarantees implemented here:

* Submissions are versioned by (source_id, revision).  Revisions must be
  strictly increasing; re-posting the exact same payload is idempotent;
  posting different content under an existing (source_id, revision) is
  rejected with 409.
* As soon as a late reading/correction is *accepted*, every current interval
  of every affected device is marked ``stale`` inside the same transaction.
* The rebuild that follows runs in its own transaction per device.  A failure
  (or a process kill, or a restart) leaves the stale rows in place — old
  results never come back as current, and devices not involved keep serving.
* Undeterminable intervals are stored with ``energy_kwh = NULL``; zero is
  only ever written for a genuinely measured zero difference.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from .errors import ConflictError, RebuildFailure
from .models import HandoverIn, ReadingIn, to_utc_iso, utc_now_iso


def _hash(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------------------
# Revisioned submission handling
# ---------------------------------------------------------------------------

def _check_revision(
    conn: sqlite3.Connection,
    table: str,
    source_id: str,
    revision: int,
    content_hash: str,
) -> bool:
    """Validate the revision chain.

    Revisions must be consecutive: the first submission is revision 1 and
    every later submission must use exactly ``latest + 1``.  Re-posting the
    exact same payload under an existing revision is idempotent.  A content
    mismatch on an existing (source_id, revision) or any rollback/skipping is
    rejected with ConflictError (HTTP 409).
    """
    row = conn.execute(
        f"SELECT revision, content_hash FROM {table} WHERE source_id = ? "
        f"ORDER BY revision DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    if row is not None:
        latest = row["revision"]
        if revision < latest:
            raise ConflictError(
                f"revision rollback rejected: latest revision of source "
                f"'{source_id}' is {latest}, got {revision}"
            )
        if revision == latest:
            existing = conn.execute(
                f"SELECT content_hash FROM {table} WHERE source_id = ? AND revision = ?",
                (source_id, revision),
            ).fetchone()
            if existing["content_hash"] != content_hash:
                raise ConflictError(
                    f"content conflict: source '{source_id}' revision {revision} "
                    f"already exists with different content"
                )
            return True  # identical re-submission
        if revision != latest + 1:
            raise ConflictError(
                f"non-consecutive revision rejected: latest revision of source "
                f"'{source_id}' is {latest}, expected {latest + 1}, got {revision}"
            )
    elif revision != 1:
        raise ConflictError(
            f"first revision of source '{source_id}' must be 1, got {revision}"
        )
    return False


def _stale_current_intervals(conn: sqlite3.Connection, device_ids: list[str]) -> int:
    """Immediately revoke current-supply eligibility on affected devices."""
    if not device_ids:
        return 0
    placeholders = ",".join("?" for _ in device_ids)
    cur = conn.execute(
        f"UPDATE intervals SET status = 'stale', "
        f"superseded_at = ? WHERE status = 'current' AND device_id IN ({placeholders})",
        [utc_now_iso(), *device_ids],
    )
    return cur.rowcount


def submit_reading(conn: sqlite3.Connection, payload: ReadingIn) -> dict:
    ts_iso = to_utc_iso(payload.ts)
    content = {
        "device_id": payload.device_id,
        "ts": ts_iso,
        "meter_no": payload.meter_no,
        "cumulative_kwh": payload.cumulative_kwh,
    }
    digest = _hash(content)
    affected: list[str] = []

    conn.execute("BEGIN IMMEDIATE")
    try:
        idempotent = _check_revision(conn, "readings", payload.source_id, payload.revision, digest)
        if not idempotent:
            conn.execute(
                "INSERT INTO readings (source_id, revision, device_id, ts, meter_no, "
                "cumulative, content_hash) VALUES (?,?,?,?,?,?,?)",
                (
                    payload.source_id, payload.revision, payload.device_id, ts_iso,
                    payload.meter_no, payload.cumulative_kwh, digest,
                ),
            )
            # The new reading belongs to its own device.  A correction may also
            # move the reading to a different device, so stale whatever device
            # the previous revision pointed at.
            prev = conn.execute(
                "SELECT device_id FROM readings WHERE source_id = ? AND revision < ? "
                "ORDER BY revision DESC LIMIT 1",
                (payload.source_id, payload.revision),
            ).fetchone()
            devices = {payload.device_id}
            if prev is not None:
                devices.add(prev["device_id"])
            affected = sorted(devices)
            _stale_current_intervals(conn, affected)
            # Any conclusion that depended on these devices loses current
            # supply eligibility immediately, in this same transaction.
            _supersede_reconciliations(
                conn, affected,
                f"reading {payload.source_id} revision {payload.revision} accepted",
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "kind": "reading",
        "source_id": payload.source_id,
        "revision": payload.revision,
        "idempotent": idempotent,
        "affected_devices": affected,
    }


def submit_handover(conn: sqlite3.Connection, payload: HandoverIn) -> dict:
    ts_iso = to_utc_iso(payload.ts)
    content = {
        "device_id": payload.device_id,
        "ts": ts_iso,
        "old_meter_no": payload.old_meter_no,
        "old_final_kwh": payload.old_final_kwh,
        "new_meter_no": payload.new_meter_no,
        "new_initial_kwh": payload.new_initial_kwh,
    }
    digest = _hash(content)
    affected: list[str] = []

    conn.execute("BEGIN IMMEDIATE")
    try:
        idempotent = _check_revision(conn, "handovers", payload.source_id, payload.revision, digest)
        if not idempotent:
            conn.execute(
                "INSERT INTO handovers (source_id, revision, device_id, ts, old_meter_no, "
                "old_final, new_meter_no, new_initial, content_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    payload.source_id, payload.revision, payload.device_id, ts_iso,
                    payload.old_meter_no, payload.old_final_kwh,
                    payload.new_meter_no, payload.new_initial_kwh, digest,
                ),
            )
            prev = conn.execute(
                "SELECT device_id, old_meter_no, new_meter_no FROM handovers "
                "WHERE source_id = ? AND revision < ? ORDER BY revision DESC LIMIT 1",
                (payload.source_id, payload.revision),
            ).fetchone()
            devices = {payload.device_id}
            if prev is not None:
                devices.add(prev["device_id"])
            affected = sorted(devices)
            _stale_current_intervals(conn, affected)
            _supersede_reconciliations(
                conn, affected,
                f"handover {payload.source_id} revision {payload.revision} accepted",
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "kind": "handover",
        "source_id": payload.source_id,
        "revision": payload.revision,
        "idempotent": idempotent,
        "affected_devices": affected,
    }


# ---------------------------------------------------------------------------
# Interval computation
# ---------------------------------------------------------------------------

def _latest_readings(conn: sqlite3.Connection, device_id: str) -> list[sqlite3.Row]:
    """Authoritative readings: highest revision of every source of a device."""
    return conn.execute(
        "WITH latest AS ("
        "  SELECT source_id, MAX(revision) AS max_rev FROM readings "
        "  WHERE device_id = ? GROUP BY source_id"
        ") SELECT r.* FROM readings r JOIN latest l "
        "ON r.source_id = l.source_id AND r.revision = l.max_rev "
        "ORDER BY r.ts ASC, r.id ASC",
        (device_id,),
    ).fetchall()


def _latest_handovers(conn: sqlite3.Connection, device_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "WITH latest AS ("
        "  SELECT source_id, MAX(revision) AS max_rev FROM handovers "
        "  WHERE device_id = ? GROUP BY source_id"
        ") SELECT h.* FROM handovers h JOIN latest l "
        "ON h.source_id = l.source_id AND h.revision = l.max_rev",
        (device_id,),
    ).fetchall()


def _settle_interval(
    conn: sqlite3.Connection,
    device_id: str,
    a: sqlite3.Row,
    b: sqlite3.Row,
) -> dict:
    """Compute the consumption between two adjacent readings.

    Same meter  -> simple difference; a backward reading is undeterminable
                   (never clamped to zero).
    Cross meter -> only settle when exactly one handover of this device falls
                   strictly inside the interval and its meter numbers match
                   both ends; otherwise undeterminable ('handover missing').
    """
    common = {
        "device_id": device_id,
        "start_ts": a["ts"], "end_ts": b["ts"],
        "start_meter_no": a["meter_no"], "end_meter_no": b["meter_no"],
        "start_source_id": a["source_id"], "start_revision": a["revision"],
        "end_source_id": b["source_id"], "end_revision": b["revision"],
        "handover_source_id": None, "handover_revision": None,
    }

    if a["meter_no"] == b["meter_no"]:
        diff = b["cumulative"] - a["cumulative"]
        if diff < 0:
            return {**common, "energy_kwh": None,
                    "reason": "negative_difference: meter reading went backward"}
        return {**common, "energy_kwh": round(diff, 9), "reason": None}

    # Cross-meter interval: look for the unique matching handover inside it.
    matches = conn.execute(
        "SELECT * FROM handovers WHERE device_id = ? AND ts > ? AND ts < ? "
        "AND old_meter_no = ? AND new_meter_no = ? "
        "AND source_id IN (SELECT source_id FROM handovers WHERE device_id = ?) "
        "ORDER BY id",
        (device_id, a["ts"], b["ts"], a["meter_no"], b["meter_no"], device_id),
    ).fetchall()
    # Keep only the latest revision per handover source.
    latest: dict[str, sqlite3.Row] = {}
    for h in matches:
        cur = latest.get(h["source_id"])
        if cur is None or h["revision"] > cur["revision"]:
            latest[h["source_id"]] = h
    unique = list(latest.values())

    if len(unique) == 0:
        return {**common, "energy_kwh": None,
                "reason": "handover_missing: no matching handover inside cross-meter interval"}
    if len(unique) > 1:
        return {**common, "energy_kwh": None,
                "reason": "ambiguous_handover: more than one matching handover inside interval"}

    h = unique[0]
    seg1 = h["old_final"] - a["cumulative"]
    seg2 = b["cumulative"] - h["new_initial"]
    if seg1 < 0 or seg2 < 0:
        return {**common, "energy_kwh": None,
                "handover_source_id": h["source_id"], "handover_revision": h["revision"],
                "reason": "negative_segment: reading backward relative to handover values"}
    return {
        **common,
        "energy_kwh": round(seg1 + seg2, 9),
        "reason": None,
        "handover_source_id": h["source_id"],
        "handover_revision": h["revision"],
    }


def devices_with_stale_intervals(conn: sqlite3.Connection) -> list[str]:
    return [
        r["device_id"]
        for r in conn.execute(
            "SELECT DISTINCT device_id FROM intervals WHERE status = 'stale' ORDER BY 1"
        )
    ]


def rebuild_device(
    conn: sqlite3.Connection,
    device_id: str,
    *,
    fail_device: str | None = None,
    fail_header_devices: set[str] | None = None,
) -> int:
    """Atomically rebuild all intervals of one device.

    Previously stale rows are kept for audit; only ``current`` rows are
    replaced, and new rows are inserted as ``current`` in one transaction.
    An injected failure raises *before* commit to simulate a crash: rollback
    leaves the run at status ``running`` and every previously stale interval
    untouched.  On service restart init_db flips dangling ``running`` runs to
    ``interrupted`` without reviving any interval.
    """
    fail_header_devices = fail_header_devices or set()
    run = conn.execute(
        "INSERT INTO rebuild_runs (device_id, status) VALUES (?, 'running')",
        (device_id,),
    )
    run_id = run.lastrowid
    conn.commit()

    conn.execute("BEGIN IMMEDIATE")
    try:
        if fail_device == device_id or device_id in fail_header_devices:
            raise RebuildFailure(
                device_id,
                "injected failure before commit (test hook); stale intervals retained",
            )

        readings = _latest_readings(conn, device_id)
        # Touch the handover table so its latest revisions are considered even
        # when no cross-meter interval exists yet.
        _latest_handovers(conn, device_id)

        rows = [
            _settle_interval(conn, device_id, readings[i], readings[i + 1])
            for i in range(len(readings) - 1)
        ]

        # Stale rows stay as an audit trail; only current rows are replaced.
        conn.execute(
            "DELETE FROM intervals WHERE device_id = ? AND status = 'current'",
            (device_id,),
        )
        for r in rows:
            conn.execute(
                "INSERT INTO intervals (device_id, start_ts, end_ts, start_meter_no, "
                "end_meter_no, energy_kwh, status, reason, start_source_id, "
                "start_revision, end_source_id, end_revision, handover_source_id, "
                "handover_revision) VALUES (?,?,?,?,?,?,'current',?,?,?,?,?,?,?)",
                (
                    r["device_id"], r["start_ts"], r["end_ts"],
                    r["start_meter_no"], r["end_meter_no"], r["energy_kwh"], r["reason"],
                    r["start_source_id"], r["start_revision"],
                    r["end_source_id"], r["end_revision"],
                    r["handover_source_id"], r["handover_revision"],
                ),
            )
        conn.execute(
            "UPDATE rebuild_runs SET status = 'success', "
            "finished_at = ? WHERE id = ?",
            (utc_now_iso(), run_id),
        )
        conn.commit()
        return len(rows)
    except RebuildFailure:
        # Simulated crash: rollback and deliberately leave the run 'running'
        # so the restart path marks it 'interrupted'.  Stale rows survive.
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        conn.execute(
            "UPDATE rebuild_runs SET status = 'failed', detail = ?, "
            "finished_at = ? WHERE id = ?",
            (str(exc), utc_now_iso(), run_id),
        )
        conn.commit()
        raise


def rebuild(
    conn: sqlite3.Connection,
    device_id: str | None = None,
    *,
    devices: list[str] | None = None,
    all_devices: bool = False,
    only_stale: bool = False,
    fail_device: str | None = None,
    fail_header_devices: set[str] | None = None,
) -> dict:
    """Rebuild one device, or every device that needs it.

    ``devices`` limits the run to an explicit set (used by automatic rebuilds
    after submissions, including a device's very first reading when no stale
    rows exist yet).  By default all devices that have readings are rebuilt;
    ``only_stale=True`` limits the run to devices currently holding stale
    intervals.  A failure on one device is recorded and does not abort the
    others; stale intervals of a failed device remain queryable as stale and
    never regain current eligibility.
    """
    if device_id is not None:
        targets = [device_id]
    elif devices is not None:
        targets = list(dict.fromkeys(devices))  # de-duplicate, keep order
    elif all_devices or not only_stale:
        targets = [
            r["device_id"]
            for r in conn.execute(
                "SELECT DISTINCT device_id FROM readings ORDER BY 1"
            )
        ]
    else:
        targets = devices_with_stale_intervals(conn)

    rebuilt: list[str] = []
    failed: dict[str, str] = {}
    for dev in targets:
        try:
            n = rebuild_device(
                conn, dev,
                fail_device=fail_device,
                fail_header_devices=fail_header_devices,
            )
            rebuilt.append(f"{dev} ({n} intervals)")
        except RebuildFailure as exc:
            failed[dev] = exc.reason

    success = not failed
    run_row = conn.execute(
        "INSERT INTO rebuild_runs (device_id, status, detail, finished_at) "
        "VALUES (?,?,?,?)",
        (
            device_id,
            "success" if success else "failed",
            json.dumps({"rebuilt": rebuilt, "failed": failed}),
            utc_now_iso(),
        ),
    )
    conn.commit()
    return {
        "run_id": run_row.lastrowid,
        "success": success,
        "rebuilt_devices": rebuilt,
        "failed_devices": failed,
    }


def list_intervals(
    conn: sqlite3.Connection,
    device_id: str | None,
    status: str,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[str] = []
    if status != "all":
        clauses.append("status = ?")
        params.append(status)
    if device_id is not None:
        clauses.append("device_id = ?")
        params.append(device_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return conn.execute(
        f"SELECT * FROM intervals{where} ORDER BY device_id, start_ts, id",
        params,
    ).fetchall()


# ---------------------------------------------------------------------------
# Reconciliation (total meter vs. sub-meter totals)
# ---------------------------------------------------------------------------


def _supersede_reconciliations(
    conn: sqlite3.Connection,
    device_ids: list[str],
    reason: str,
) -> list[int]:
    """Revoke the current-supply eligibility of conclusions touching devices.

    Called inside the same transaction that accepts a reading or handover, so
    a conclusion can never remain current across an accepted correction.  The
    rows are kept (status='superseded') and point at the accepted revision.
    """
    if not device_ids:
        return []
    placeholders = ",".join("?" for _ in device_ids)
    recon_ids = [
        r["reconciliation_id"]
        for r in conn.execute(
            f"SELECT DISTINCT reconciliation_id FROM reconciliation_devices "
            f"WHERE device_id IN ({placeholders})",
            list(device_ids),
        )
    ]
    if not recon_ids:
        return []
    id_params = ",".join("?" for _ in recon_ids)
    conn.execute(
        f"UPDATE reconciliations SET status = 'superseded', superseded_at = ?, "
        f"superseded_reason = ? "
        f"WHERE id IN ({id_params}) AND status IN ('published','blocked')",
        [utc_now_iso(), reason, *recon_ids],
    )
    return recon_ids


def reconciliation_fingerprint(
    total_device_id: str,
    sub_device_ids: list[str],
    start_ts: str,
    end_ts: str,
    tolerance_kwh: float,
) -> str:
    return _hash(
        {
            "total_device_id": total_device_id,
            "sub_device_ids": sorted(sub_device_ids),
            "start_ts": start_ts,
            "end_ts": end_ts,
            "tolerance_kwh": tolerance_kwh,
        }
    )


def _reading_view(row: sqlite3.Row, with_device: bool = False) -> dict:
    view = {
        "source_id": row["source_id"],
        "revision": row["revision"],
        "ts": row["ts"],
        "meter_no": row["meter_no"],
        "cumulative_kwh": row["cumulative"],
    }
    if with_device:
        view["device_id"] = row["device_id"]
    return view


def _handover_view(row: sqlite3.Row) -> dict:
    return {
        "source_id": row["source_id"],
        "revision": row["revision"],
        "device_id": row["device_id"],
        "ts": row["ts"],
        "old_meter_no": row["old_meter_no"],
        "old_final_kwh": row["old_final"],
        "new_meter_no": row["new_meter_no"],
        "new_initial_kwh": row["new_initial"],
    }


def _handover_for_segment(
    conn: sqlite3.Connection,
    device_id: str,
    start_ts: str,
    end_ts: str,
    start_meter_no: str,
    end_meter_no: str,
) -> sqlite3.Row | None:
    """The unique handover actually used by a segment, if any.

    Mirrors ``_settle_interval``: the handover must fall strictly between the
    bounding readings and match the segment's meter pair.  A same-meter
    segment therefore never reports a handover.
    """
    matches = conn.execute(
        "SELECT * FROM handovers WHERE device_id = ? AND ts > ? AND ts < ? "
        "AND old_meter_no = ? AND new_meter_no = ? ORDER BY id",
        (device_id, start_ts, end_ts, start_meter_no, end_meter_no),
    ).fetchall()
    latest: dict[str, sqlite3.Row] = {}
    for h in matches:
        cur = latest.get(h["source_id"])
        if cur is None or h["revision"] > cur["revision"]:
            latest[h["source_id"]] = h
    unique = list(latest.values())
    return unique[0] if len(unique) == 1 else None


def _evaluate_device_window(
    conn: sqlite3.Connection, device_id: str, start_ts: str, end_ts: str
) -> dict:
    """Check one device's current supply data covers [start_ts, end_ts].

    The result is either ``{"ok": True, "chain": [...], "segments": [...]}``
    where every segment is determinable, or ``{"ok": False, "reason": ...,
    "detail": ...}``.  Readings are the authoritative latest revision per
    source; the *current* interval rows must match the chain settled from
    those readings exactly.  Historical stale rows are kept for audit and do
    not by themselves mean a rebuild is pending.
    """
    readings = _latest_readings(conn, device_id)
    if not readings:
        return {"ok": False, "reason": "incomplete_coverage", "detail": "device has no readings"}

    before = [r for r in readings if r["ts"] <= start_ts]
    after = [r for r in readings if r["ts"] >= end_ts]
    if not before:
        return {
            "ok": False,
            "reason": "incomplete_coverage",
            "detail": "no reading at or before start_ts",
        }
    if not after:
        return {
            "ok": False,
            "reason": "incomplete_coverage",
            "detail": "no reading at or after end_ts",
        }

    a, b = before[-1], after[0]
    for boundary_ts in (a["ts"], b["ts"]):
        same_ts = [r for r in readings if r["ts"] == boundary_ts]
        if len(same_ts) > 1:
            return {
                "ok": False,
                "reason": "ambiguous_boundary",
                "detail": (
                    f"{len(same_ts)} authoritative readings share boundary "
                    f"timestamp {boundary_ts}"
                ),
            }
    inner = [r for r in readings if start_ts < r["ts"] < end_ts]
    chain = [a, *inner, b]
    stamps = [r["ts"] for r in chain]
    if len(set(stamps)) != len(stamps):
        return {
            "ok": False,
            "reason": "ambiguous_boundary",
            "detail": "more than one authoritative reading shares an interior segment boundary",
        }

    segments = [
        _settle_interval(conn, device_id, chain[i], chain[i + 1])
        for i in range(len(chain) - 1)
    ]

    # The settled view must be backed by current intervals (not just raw
    # readings): require the current interval rows to match the boundary chain
    # exactly, so a pending/damaged rebuild can never silently pass.
    current_rows = conn.execute(
        "SELECT start_ts, end_ts, start_source_id, start_revision, "
        "end_source_id, end_revision FROM intervals "
        "WHERE device_id = ? AND status = 'current' AND start_ts < ? AND end_ts > ? "
        "ORDER BY start_ts, id",
        (device_id, end_ts, start_ts),
    ).fetchall()
    expected = [
        (
            chain[i]["ts"], chain[i + 1]["ts"],
            chain[i]["source_id"], chain[i]["revision"],
            chain[i + 1]["source_id"], chain[i + 1]["revision"],
        )
        for i in range(len(chain) - 1)
    ]
    actual = [
        (
            r["start_ts"], r["end_ts"],
            r["start_source_id"], r["start_revision"],
            r["end_source_id"], r["end_revision"],
        )
        for r in current_rows
    ]
    if actual != expected:
        stale = conn.execute(
            "SELECT COUNT(*) AS n FROM intervals WHERE device_id = ? "
            "AND status = 'stale' AND start_ts < ? AND end_ts > ?",
            (device_id, end_ts, start_ts),
        ).fetchone()["n"]
        if stale:
            return {
                "ok": False,
                "reason": "pending_rebuild",
                "detail": (
                    f"{stale} stale interval(s) overlap the requested window and "
                    f"current intervals do not cover it; rebuild required"
                ),
            }
        return {
            "ok": False,
            "reason": "current_intervals_unavailable",
            "detail": "current intervals do not match the authoritatively bounded window",
        }

    undeterminable = [s for s in segments if s["energy_kwh"] is None]
    if undeterminable:
        first = undeterminable[0]
        return {
            "ok": False,
            "reason": "undeterminable",
            "detail": (
                f"energy undeterminable for {first['start_ts']}..{first['end_ts']}: "
                f"{first['reason']}"
            ),
            "undeterminable_segments": [
                {
                    "start_ts": seg["start_ts"],
                    "end_ts": seg["end_ts"],
                    "reason": seg["reason"],
                }
                for seg in undeterminable
            ],
        }
    return {"ok": True, "chain": chain, "segments": segments}


def reconcile(
    conn: sqlite3.Connection,
    total_device_id: str,
    sub_device_ids: list[str],
    start_ts: str,
    end_ts: str,
    tolerance_kwh: float,
) -> dict:
    """Evaluate and persist one reconciliation conclusion atomically.

    The whole evaluation reads a single committed snapshot (BEGIN IMMEDIATE)
    and stores the conclusion in the same transaction.  If any participating
    device cannot fully and unambiguously cover the window, nothing is
    published: the row is stored as 'blocked' with the blocked devices.
    """
    fingerprint = reconciliation_fingerprint(
        total_device_id, sub_device_ids, start_ts, end_ts, tolerance_kwh
    )
    # Canonical order keeps stored output stable regardless of request order.
    sub_device_ids = sorted(sub_device_ids)
    devices = [("total", total_device_id)] + [("sub", d) for d in sub_device_ids]
    participating = [total_device_id, *sub_device_ids]

    conn.execute("BEGIN IMMEDIATE")
    try:
        version = conn.execute(
            "SELECT COUNT(*) AS n FROM reconciliations WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()["n"] + 1

        # A new reconciliation replaces the previous conclusion for the same
        # parameters; the old version stays queryable but is no longer current.
        conn.execute(
            "UPDATE reconciliations SET status = 'superseded', superseded_at = ?, "
            "superseded_reason = 'replaced by newer reconciliation' "
            "WHERE fingerprint = ? AND status IN ('published','blocked')",
            (utc_now_iso(), fingerprint),
        )

        evaluations = {
            dev: _evaluate_device_window(conn, dev, start_ts, end_ts)
            for dev in participating
        }
        blockers: dict[str, str] = {}
        blocker_details: dict[str, list] = {}
        for dev, ev in evaluations.items():
            if not ev["ok"]:
                blockers[dev] = f"{ev['reason']}: {ev['detail']}"
                blocker_details.setdefault(dev, ev.get("undeterminable_segments", []))

        total_eval = evaluations[total_device_id]
        if not blockers:
            # Boundary consistency: every device must use exactly the same
            # segment boundaries as the total meter.
            reference = [r["ts"] for r in total_eval["chain"]]
            for dev in sub_device_ids:
                if [r["ts"] for r in evaluations[dev]["chain"]] != reference:
                    blockers[dev] = (
                        "boundary_mismatch: segment boundaries differ from the total meter"
                    )
                    blocker_details[dev] = [
                        {"reason": "boundary_mismatch: boundaries differ from the total meter"}
                    ]

        if blockers:
            reason_text = "; ".join(f"{dev} -> {why}" for dev, why in sorted(blockers.items()))
            cur = conn.execute(
                "INSERT INTO reconciliations (total_device_id, sub_device_ids, start_ts, "
                "end_ts, tolerance_kwh, fingerprint, version, status, segments, "
                "blocked_devices, exceeded, detail) "
                "VALUES (?,?,?,?,?,?,?,'blocked',NULL,?,NULL,?)",
                (
                    total_device_id,
                    json.dumps(sub_device_ids),
                    start_ts, end_ts, tolerance_kwh, fingerprint, version,
                    json.dumps(
                        [
                            {"device_id": d, **blocker_details.get(d, [{}])[0]}
                            if blocker_details.get(d)
                            else {"device_id": d}
                            for d in participating if d in blockers
                        ]
                    ),
                    f"reconciliation blocked; no partial conclusion published: {reason_text}",
                ),
            )
            recon_id = cur.lastrowid
            _insert_reconciliation_devices(conn, recon_id, devices)
            conn.commit()
            return _load_reconciliation(conn, recon_id)

        total_segments = total_eval["segments"]
        sub_segments = {d: evaluations[d]["segments"] for d in sub_device_ids}
        out_segments: list[dict] = []
        for idx, seg in enumerate(total_segments):
            total_kwh = seg["energy_kwh"]
            sub_kwh = round(sum(sub_segments[d][idx]["energy_kwh"] for d in sub_device_ids), 9)
            difference = round(total_kwh - sub_kwh, 9)
            total_start = total_eval["chain"][idx]
            total_end = total_eval["chain"][idx + 1]

            sub_readings = []
            sub_handovers = []
            for d in sub_device_ids:
                d_chain = evaluations[d]["chain"]
                sub_readings.append(_reading_view(d_chain[idx], with_device=True))
                sub_readings.append(_reading_view(d_chain[idx + 1], with_device=True))
                h = _handover_for_segment(
                    conn, d, d_chain[idx]["ts"], d_chain[idx + 1]["ts"],
                    d_chain[idx]["meter_no"], d_chain[idx + 1]["meter_no"],
                )
                if h is not None:
                    sub_handovers.append(_handover_view(h))

            total_handover = _handover_for_segment(
                conn, total_device_id, total_start["ts"], total_end["ts"],
                total_start["meter_no"], total_end["meter_no"],
            )
            out_segments.append(
                {
                    "start_ts": total_start["ts"],
                    "end_ts": total_end["ts"],
                    "total_kwh": total_kwh,
                    "sub_kwh": sub_kwh,
                    "difference_kwh": difference,
                    "exceeded": abs(difference) > tolerance_kwh + 1e-9,
                    "total_readings": [
                        _reading_view(total_start),
                        _reading_view(total_end),
                    ],
                    "sub_readings": sub_readings,
                    "total_handover": (
                        _handover_view(total_handover) if total_handover is not None else None
                    ),
                    "sub_handovers": sub_handovers,
                }
            )

        exceeded = any(s["exceeded"] for s in out_segments)
        cur = conn.execute(
            "INSERT INTO reconciliations (total_device_id, sub_device_ids, start_ts, "
            "end_ts, tolerance_kwh, fingerprint, version, status, segments, "
            "blocked_devices, exceeded, detail) "
            "VALUES (?,?,?,?,?,?,?,'published',?,'[]',?,?)",
            (
                total_device_id,
                json.dumps(sub_device_ids),
                start_ts, end_ts, tolerance_kwh, fingerprint, version,
                json.dumps(out_segments),
                1 if exceeded else 0,
                "reconciliation published",
            ),
        )
        recon_id = cur.lastrowid
        _insert_reconciliation_devices(conn, recon_id, devices)
        conn.commit()
        return _load_reconciliation(conn, recon_id)
    except Exception:
        conn.rollback()
        raise


def _insert_reconciliation_devices(
    conn: sqlite3.Connection, recon_id: int, devices: list[tuple[str, str]]
) -> None:
    conn.executemany(
        "INSERT INTO reconciliation_devices (reconciliation_id, device_id, role) "
        "VALUES (?,?,?)",
        [(recon_id, dev, role) for role, dev in devices],
    )


def _reconciliation_out(row: sqlite3.Row) -> dict:
    segments = json.loads(row["segments"]) if row["segments"] else None
    raw_blocked = json.loads(row["blocked_devices"] or "[]")
    # Stored as objects {device_id, start_ts?, end_ts?, reason?}; the public
    # blocked_devices list stays a clean list of device ids.
    blocked_details = [b for b in raw_blocked if isinstance(b, dict)]
    blocked_devices = [
        b if isinstance(b, str) else b["device_id"] for b in raw_blocked
    ]
    return {
        "id": row["id"],
        "status": row["status"],
        "total_device_id": row["total_device_id"],
        "sub_device_ids": json.loads(row["sub_device_ids"]),
        "start_ts": row["start_ts"],
        "end_ts": row["end_ts"],
        "tolerance_kwh": row["tolerance_kwh"],
        "fingerprint": row["fingerprint"],
        "version": row["version"],
        "segments": segments,
        "exceeded": None if row["exceeded"] is None else bool(row["exceeded"]),
        "blocked_devices": blocked_devices,
        "blocked_details": blocked_details,
        "detail": row["detail"],
        "created_at": row["created_at"],
        "superseded_at": row["superseded_at"],
        "superseded_reason": row["superseded_reason"],
    }


def _load_reconciliation(conn: sqlite3.Connection, recon_id: int) -> dict:
    row = conn.execute("SELECT * FROM reconciliations WHERE id = ?", (recon_id,)).fetchone()
    if row is None:
        return None
    return _reconciliation_out(row)


def get_reconciliation(
    conn: sqlite3.Connection, recon_id: int, include_superseded: bool = False
) -> dict | None:
    row = conn.execute("SELECT * FROM reconciliations WHERE id = ?", (recon_id,)).fetchone()
    if row is None:
        return None
    if row["status"] == "superseded" and not include_superseded:
        return None
    return _reconciliation_out(row)


def latest_reconciliation(
    conn: sqlite3.Connection, fingerprint: str
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM reconciliations WHERE fingerprint = ? AND status IN ('published','blocked') "
        "ORDER BY version DESC, id DESC LIMIT 1",
        (fingerprint,),
    ).fetchone()
    return _reconciliation_out(row) if row is not None else None


def list_reconciliations(
    conn: sqlite3.Connection,
    *,
    total_device_id: str | None = None,
    sub_device_id: str | None = None,
    start_ts: str | None = None,
    end_ts: str | None = None,
    tolerance_kwh: float | None = None,
    status: str = "current",
    limit: int = 50,
) -> list[dict]:
    clauses: list[str] = []
    params: list = []
    if total_device_id is not None:
        clauses.append("r.total_device_id = ?")
        params.append(total_device_id)
    if sub_device_id is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM reconciliation_devices rd WHERE "
            "rd.reconciliation_id = r.id AND rd.device_id = ? AND rd.role = 'sub')"
        )
        params.append(sub_device_id)
    if start_ts is not None:
        clauses.append("r.start_ts = ?")
        params.append(start_ts)
    if end_ts is not None:
        clauses.append("r.end_ts = ?")
        params.append(end_ts)
    if tolerance_kwh is not None:
        clauses.append("r.tolerance_kwh = ?")
        params.append(tolerance_kwh)
    if status == "current":
        clauses.append("r.status IN ('published','blocked')")
    elif status != "all":
        clauses.append("r.status = ?")
        params.append(status)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"SELECT r.* FROM reconciliations r{where} "
        f"ORDER BY r.id DESC LIMIT ?",
        params,
    ).fetchall()
    return [_reconciliation_out(r) for r in rows]
