"""Pydantic request/response models and shared helpers."""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def to_utc_iso(dt: datetime) -> str:
    """Normalize any aware datetime to a UTC ISO-8601 string."""
    if dt.tzinfo is None:
        raise ValueError("timestamp must include a timezone (use UTC, e.g. 2026-01-01T00:00:00Z)")
    dt_utc = dt.astimezone(timezone.utc)
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class ReadingIn(BaseModel):
    source_id: str = Field(min_length=1, description="来源编号，同一读数来源的唯一标识")
    revision: int = Field(ge=1, description="递增修订号，首次为 1，再次提交必须 +1")
    device_id: str = Field(min_length=1)
    ts: datetime = Field(description="读数时刻（UTC，需带时区）")
    meter_no: str = Field(min_length=1)
    cumulative_kwh: float = Field(ge=0, description="累计电表读数 kWh（非负）")

    @field_validator("source_id", "device_id", "meter_no")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class HandoverIn(BaseModel):
    source_id: str = Field(min_length=1, description="来源编号")
    revision: int = Field(ge=1, description="递增修订号")
    device_id: str = Field(min_length=1)
    ts: datetime = Field(description="换表时刻（UTC，需带时区）")
    old_meter_no: str = Field(min_length=1)
    old_final_kwh: float = Field(ge=0, description="旧表末值（累计 kWh）")
    new_meter_no: str = Field(min_length=1)
    new_initial_kwh: float = Field(ge=0, description="新表初值（累计 kWh）")

    @field_validator("source_id", "device_id", "old_meter_no", "new_meter_no")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("new_meter_no")
    @classmethod
    def _distinct(cls, v: str, info):
        old = info.data.get("old_meter_no")
        if old is not None and v == old:
            raise ValueError("new_meter_no must differ from old_meter_no")
        return v


class AcceptOut(BaseModel):
    accepted: bool
    kind: str  # reading | handover
    source_id: str
    revision: int
    idempotent: bool = False
    affected_devices: list[str]
    rebuild: dict


class IntervalOut(BaseModel):
    id: int
    device_id: str
    start_ts: str
    end_ts: str
    start_meter_no: str
    end_meter_no: str
    energy_kwh: float | None
    undeterminable: bool
    reason: str | None
    status: str
    start_source_id: str
    start_revision: int
    end_source_id: str
    end_revision: int
    handover_source_id: str | None
    handover_revision: int | None


class RebuildOut(BaseModel):
    run_id: int
    success: bool
    rebuilt_devices: list[str]
    failed_devices: dict[str, str]
    detail: str | None = None


class ReconciliationIn(BaseModel):
    total_device_id: str = Field(min_length=1, description="总表设备号")
    sub_device_ids: list[str] = Field(min_length=1, description="分表设备号（至少一台）")
    start_ts: datetime = Field(description="对账区间起（UTC，闭区间）")
    end_ts: datetime = Field(description="对账区间止（UTC，闭区间）")
    tolerance_kwh: float = Field(ge=0, description="允许差额 kWh（非负）")

    @field_validator("start_ts", "end_ts")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError(
                "timestamp must include a timezone (use UTC, e.g. 2026-01-01T00:00:00Z)"
            )
        return v

    @field_validator("total_device_id")
    @classmethod
    def _strip_total(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("sub_device_ids")
    @classmethod
    def _clean_subs(cls, v: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in v:
            item = raw.strip()
            if not item:
                raise ValueError("sub_device_ids must not contain blank values")
            if item not in cleaned:
                cleaned.append(item)
        if not cleaned:
            raise ValueError("sub_device_ids must contain at least one device")
        return cleaned


class ReconciliationSegmentOut(BaseModel):
    start_ts: str = Field(description="分段起点（总表读数时刻，UTC）")
    end_ts: str = Field(description="分段终点（总表读数时刻，UTC）")
    total_kwh: float = Field(description="本段总表电量")
    sub_kwh: float = Field(description="本段各分表电量合计")
    difference_kwh: float = Field(description="总表减分表合计")
    exceeded: bool = Field(description="|difference_kwh| 是否超过允许差额")
    total_readings: list[dict] = Field(description="总表本段两端读数及来源编号/修订号")
    sub_readings: list[dict] = Field(description="各分表本段两端读数、设备号及来源编号/修订号")
    total_handover: dict | None = Field(description="总表本段使用的换表交接（无则为 null）")
    sub_handovers: list[dict] = Field(description="各分表本段使用的换表交接")


class ReconciliationOut(BaseModel):
    id: int
    status: str = Field(description="published | blocked | superseded")
    total_device_id: str
    sub_device_ids: list[str]
    start_ts: str
    end_ts: str
    tolerance_kwh: float
    fingerprint: str
    version: int | None
    segments: list[ReconciliationSegmentOut] | None = Field(
        description="逐段结论；未发布（blocked/superseded-without-segments）时为 null，绝不返回部分结论"
    )
    exceeded: bool | None = Field(description="任一段超限即为 true；未发布时为 null")
    blocked_devices: list[str] = Field(description="受阻设备（含总表与分表）")
    blocked_details: list[dict] = Field(
        default_factory=list, description="受阻明细（设备、原因、涉及分段）"
    )
    detail: str | None
    created_at: str | None
    superseded_at: str | None
    superseded_reason: str | None


class ReconciliationListOut(BaseModel):
    items: list[ReconciliationOut]
