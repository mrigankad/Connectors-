"""Manufacturing domain. Preserves plant, warehouse and effective dates. Unknown unit conversion fails."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import Field

from .common import DomainRecord, Money


class UoM(DomainRecord):
    code: str
    name: str
    base_code: str | None = None
    factor_to_base: Decimal | None = Field(default=None, description="Approved conversion; None means unknown")


class Material(DomainRecord):
    material_id: str
    description: str
    base_uom: str
    plant: str | None = None
    material_type: str | None = None


class BOMRevision(DomainRecord):
    bom_id: str
    revision: str
    parent_material_id: str
    plant: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    components: list[dict[str, str]] = Field(default_factory=list, description="material_id, quantity, uom")


class StockPosition(DomainRecord):
    material_id: str
    plant: str
    warehouse: str | None = None
    quantity: Decimal
    uom: str
    as_of_iso: str


class Lot(DomainRecord):
    lot_id: str
    material_id: str
    plant: str | None = None
    quantity: Decimal
    uom: str
    expiry: date | None = None


class SalesOrder(DomainRecord):
    order_id: str
    customer_id: str | None = None
    order_date: date
    lines: list[dict[str, str]] = Field(default_factory=list)


class PurchaseOrder(DomainRecord):
    order_id: str
    supplier_id: str | None = None
    order_date: date
    total: Money | None = None
    lines: list[dict[str, str]] = Field(default_factory=list)


class WorkOrder(DomainRecord):
    work_order_id: str
    material_id: str
    plant: str
    quantity: Decimal
    uom: str
    status: str | None = None
    planned_start: date | None = None
    planned_end: date | None = None


class QualityResult(DomainRecord):
    result_id: str
    lot_id: str | None = None
    work_order_id: str | None = None
    characteristic: str
    value: str
    uom: str | None = None
    passed: bool | None = None
