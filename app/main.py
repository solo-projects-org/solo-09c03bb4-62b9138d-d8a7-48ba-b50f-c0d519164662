"""FastAPI application: readings, handovers, intervals, rebuild, reconciliation.

Swagger UI is served at ``/docs`` (ReDoc at ``/redoc``) and the OpenAPI
document at ``/openapi.json``.
"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .database import ConnectionPool
from .errors import ConflictError, NotFoundError, RebuildFailure
from .models import (
    AcceptOut,
    HandoverIn,
    IntervalOut,
    ReadingIn,
    RebuildOut,
    ReconciliationIn,
    ReconciliationListOut,
    ReconciliationOut,
    to_utc_iso,
)
from . import service


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Per-request SQLite connections; WAL + busy_timeout guard writers.
        app.state.pool = ConnectionPool(settings.db_path)
        app.state.pool.init_schema()
        # Deliberately NO automatic rebuild on startup: a restart must never
        # resurrect intervals that lost their current-supply eligibility.
        yield
        app.state.pool.close()

    app = FastAPI(
        title="工厂能耗区间结算 API",
        description=(
            "提交按设备区分的累计电表读数与换表交接，按相邻读数生成带来源编号的"
            "区间电量。倒退读数或交接缺失记为无法判定（energy_kwh=null），绝不报零。"
            "新增区间对账：总表减分表合计的逐段差额、超限判定与读数/交接来源；"
            "缺段、无法判定或待重建时不发布部分结论，并列出受阻设备。"
            "任一参与设备的读数或交接更正一经受理，旧结论立即失去当前供数资格，"
            "只有重新对账才会生成新版本，旧版本可追溯。"
        ),
        version="1.1.0",
        lifespan=lifespan,
    )

    def db(request: Request):
        # One connection per request, returned to the pool afterwards.  A
        # sync dependency and its endpoint may run on different worker
        # threads; a per-request connection is never used concurrently.
        conn = request.app.state.pool.acquire()
        try:
            yield conn
        finally:
            request.app.state.pool.release(conn)

    @app.exception_handler(ConflictError)
    async def conflict_handler(request: Request, exc: ConflictError):
        return JSONResponse(status_code=409, content={"detail": exc.detail})

    @app.exception_handler(NotFoundError)
    async def not_found_handler(request: Request, exc: NotFoundError):
        return JSONResponse(status_code=404, content={"detail": exc.detail})

    @app.get("/health", tags=["运维"])
    def health(conn: sqlite3.Connection = Depends(db)):
        row = conn.execute("SELECT COUNT(*) AS n FROM intervals WHERE status='current'").fetchone()
        stale = conn.execute("SELECT COUNT(*) AS n FROM intervals WHERE status='stale'").fetchone()
        return {"status": "ok", "current_intervals": row["n"], "stale_intervals": stale["n"]}

    @app.post("/readings", response_model=AcceptOut, status_code=201, tags=["读数"])
    def post_reading(
        payload: ReadingIn,
        request: Request,
        conn: sqlite3.Connection = Depends(db),
        x_fail_rebuild: bool = Header(default=False, include_in_schema=False),
    ):
        result = service.submit_reading(conn, payload)
        rebuild_info = _auto_rebuild(request, conn, result["affected_devices"], x_fail_rebuild)
        return {**result, "accepted": True, "rebuild": rebuild_info}

    @app.post("/handovers", response_model=AcceptOut, status_code=201, tags=["换表交接"])
    def post_handover(
        payload: HandoverIn,
        request: Request,
        conn: sqlite3.Connection = Depends(db),
        x_fail_rebuild: bool = Header(default=False, include_in_schema=False),
    ):
        result = service.submit_handover(conn, payload)
        rebuild_info = _auto_rebuild(request, conn, result["affected_devices"], x_fail_rebuild)
        return {**result, "accepted": True, "rebuild": rebuild_info}

    def _auto_rebuild(request: Request, conn: sqlite3.Connection, devices: list[str], fail: bool) -> dict:
        if not devices:
            return {"triggered": False}
        fail_set = set(devices) if fail else set()
        out = service.rebuild(
            conn,
            devices=devices,
            fail_device=request.app.state.settings.fail_device,
            fail_header_devices=fail_set,
        )
        out["triggered"] = True
        return out

    @app.post("/rebuild", response_model=RebuildOut, tags=["区间重建"])
    def post_rebuild(
        request: Request,
        device_id: str | None = Query(default=None, description="只重建指定设备"),
        all_devices: bool = Query(default=False, description="强制重建全部设备（默认只重建存在 stale 区间的设备）"),
        x_fail_rebuild: bool = Header(default=False, include_in_schema=False),
        conn: sqlite3.Connection = Depends(db),
    ):
        fail_set: set[str] = set()
        if x_fail_rebuild:
            if device_id:
                fail_set.add(device_id)
            else:
                fail_set.update(service.devices_with_stale_intervals(conn))
        try:
            return service.rebuild(
                conn, device_id=device_id, all_devices=all_devices,
                only_stale=not all_devices and device_id is None,
                fail_device=request.app.state.settings.fail_device,
                fail_header_devices=fail_set,
            )
        except RebuildFailure as exc:  # pragma: no cover - rebuild() records per-device failures
            return JSONResponse(status_code=500, content={"detail": str(exc)})

    @app.get("/intervals", response_model=list[IntervalOut], tags=["区间查询"])
    def get_intervals(
        device_id: str | None = Query(default=None),
        status: str = Query(default="current", pattern="^(current|stale|all)$"),
        conn: sqlite3.Connection = Depends(db),
    ):
        rows = service.list_intervals(conn, device_id, status)
        return [
            {
                "id": r["id"],
                "device_id": r["device_id"],
                "start_ts": r["start_ts"],
                "end_ts": r["end_ts"],
                "start_meter_no": r["start_meter_no"],
                "end_meter_no": r["end_meter_no"],
                "energy_kwh": r["energy_kwh"],
                "undeterminable": r["energy_kwh"] is None,
                "reason": r["reason"],
                "status": r["status"],
                "start_source_id": r["start_source_id"],
                "start_revision": r["start_revision"],
                "end_source_id": r["end_source_id"],
                "end_revision": r["end_revision"],
                "handover_source_id": r["handover_source_id"],
                "handover_revision": r["handover_revision"],
            }
            for r in rows
        ]

    @app.get("/rebuild-runs", tags=["运维"])
    def get_rebuild_runs(conn: sqlite3.Connection = Depends(db)):
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM rebuild_runs ORDER BY id DESC LIMIT 50"
            ).fetchall()
        ]

    # ------------------------------------------------------------------
    # Reconciliation: total meter vs. sub-meter total
    # ------------------------------------------------------------------

    @app.post(
        "/reconciliations",
        response_model=ReconciliationOut,
        status_code=201,
        tags=["区间对账"],
        summary="提交一次区间对账（发布结论，否则仅记录受阻）",
    )
    def post_reconciliation(
        payload: ReconciliationIn,
        conn: sqlite3.Connection = Depends(db),
    ):
        start_ts = to_utc_iso(payload.start_ts)
        end_ts = to_utc_iso(payload.end_ts)
        if start_ts >= end_ts:
            raise HTTPException(
                status_code=422, detail="end_ts must be strictly after start_ts"
            )
        if payload.total_device_id in payload.sub_device_ids:
            raise HTTPException(
                status_code=422,
                detail="total_device_id must not also appear in sub_device_ids",
            )
        return service.reconcile(
            conn,
            payload.total_device_id,
            payload.sub_device_ids,
            start_ts,
            end_ts,
            payload.tolerance_kwh,
        )

    @app.get(
        "/reconciliations/latest",
        response_model=ReconciliationOut,
        tags=["区间对账"],
        summary="按对账参数查询当前结论（只返回仍有供数资格的结论）",
    )
    def get_latest_reconciliation(
        total_device_id: str = Query(description="总表设备号"),
        sub_device_ids: str = Query(description="分表设备号，逗号分隔"),
        start_ts: datetime = Query(description="UTC 起（含）"),
        end_ts: datetime = Query(description="UTC 止（含）"),
        tolerance_kwh: float = Query(description="允许差额 kWh"),
        conn: sqlite3.Connection = Depends(db),
    ):
        normalized_start, normalized_end = _normalize_query_window(start_ts, end_ts)
        subs = _parse_subs(sub_device_ids)
        fingerprint = service.reconciliation_fingerprint(
            total_device_id, subs, normalized_start, normalized_end, tolerance_kwh
        )
        conclusion = service.latest_reconciliation(conn, fingerprint)
        if conclusion is None:
            raise NotFoundError(
                "no current reconciliation for these parameters; "
                "it may never have been submitted or may have been superseded"
            )
        return conclusion

    @app.get(
        "/reconciliations",
        response_model=ReconciliationListOut,
        tags=["区间对账"],
        summary="查询对账结论（默认仅当前结论；status=all 可追溯历史版本）",
    )
    def get_reconciliations(
        total_device_id: str | None = Query(default=None),
        sub_device_id: str | None = Query(default=None, description="只要涉及该分表设备"),
        start_ts: datetime | None = Query(default=None),
        end_ts: datetime | None = Query(default=None),
        tolerance_kwh: float | None = Query(default=None),
        status: str = Query(default="current", pattern="^(current|published|blocked|superseded|all)$"),
        limit: int = Query(default=50, ge=1, le=500),
        conn: sqlite3.Connection = Depends(db),
    ):
        normalized_start = _normalize_optional_ts(start_ts)
        normalized_end = _normalize_optional_ts(end_ts)
        items = service.list_reconciliations(
            conn,
            total_device_id=total_device_id,
            sub_device_id=sub_device_id,
            start_ts=normalized_start,
            end_ts=normalized_end,
            tolerance_kwh=tolerance_kwh,
            status=status,
            limit=limit,
        )
        return {"items": items}

    @app.get(
        "/reconciliations/{recon_id}",
        response_model=ReconciliationOut,
        tags=["区间对账"],
        summary="按编号追溯一条对账结论（含已失效版本）",
    )
    def get_reconciliation(recon_id: int, conn: sqlite3.Connection = Depends(db)):
        conclusion = service.get_reconciliation(conn, recon_id, include_superseded=True)
        if conclusion is None:
            raise NotFoundError(f"reconciliation {recon_id} not found")
        return conclusion

    def _normalize_optional_ts(value: datetime | None) -> str | None:
        if value is None:
            return None
        try:
            return to_utc_iso(value)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    def _normalize_query_window(start: datetime, end: datetime) -> tuple[str, str]:
        normalized_start = _normalize_optional_ts(start)
        normalized_end = _normalize_optional_ts(end)
        if normalized_start >= normalized_end:
            raise HTTPException(
                status_code=422, detail="end_ts must be strictly after start_ts"
            )
        return normalized_start, normalized_end

    def _parse_subs(raw: str) -> list[str]:
        subs = [part.strip() for part in raw.split(",") if part.strip()]
        if not subs:
            raise HTTPException(
                status_code=422, detail="sub_device_ids must contain at least one device"
            )
        return list(dict.fromkeys(subs))

    app.state.settings = settings
    return app


app = create_app()
