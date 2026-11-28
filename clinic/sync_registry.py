"""What syncs, and how each kind of row merges (docs/OFFLINE_SYNC_CONTRACT.md, section 8.3).

Every entity lists the plain columns that travel, the relations (sent as the related row's
`sync_uuid`), the user columns (sent as usernames), and its merge rule:

  append   Payments, refunds, stock movements, damages: insert if the uuid is new, never update.
  forward  Encounters, orders, billable lines: the status only moves forward.
  catalog  Patients and catalog: the cloud wins on pull; local edits upsert on push; both sides
           changed means a conflict is logged and the cloud copy is kept.
  lww      Clinical documents (charts, notes, results): the newest edit wins.
"""
from dataclasses import dataclass

from django.db.models import Q

from . import models as m

APPEND, FORWARD, CATALOG, LWW = "append", "forward", "catalog", "lww"


@dataclass(frozen=True)
class Entity:
    key: str
    model: type
    fields: tuple
    rule: str
    direct: bool = True  # carries clinic_tin / branch_name itself
    parent: str = ""  # ORM path to the scoped parent when not direct, e.g. "encounter__"
    refs: tuple = ()  # ((field, target entity key, required), ...)
    users: tuple = ()  # user FK fields, exchanged as usernames
    natural: tuple = ()  # business key used to recognise "the same row" created on both sides
    stamp: str = "updated_at"  # column that orders changes
    status_field: str = ""
    ranks: tuple = ()  # status order for the "forward" rule, lowest first
    restore: tuple = ()  # auto_now_add columns to put back after an insert


ENTITIES = (
    Entity("departments", m.Department, ("name", "is_active", "created_at"), CATALOG,
           natural=("name",), restore=("created_at",)),
    Entity("suppliers", m.Supplier, ("supplier_name", "supplier_phone", "supplier_email", "created_at"), CATALOG,
           natural=("supplier_name",), restore=("created_at",)),
    Entity("services", m.BillableService,
           ("code", "name", "description", "department", "service_type", "unit_price", "default_quantity",
            "is_active", "auto_add_on_registration", "requires_payment_before_work", "internal_notes",
            "includes_referral_patients"),
           CATALOG, natural=("code",)),
    Entity("lab_parameters", m.LabTestParameter,
           ("name", "result_type", "unit", "ref_low", "ref_high", "critical_low", "critical_high",
            "choices", "abnormal_choices", "sort_order"),
           CATALOG, direct=False, parent="service__", refs=(("service", "services", True),)),
    # Stock columns (on_hand, loose_stock) are deliberately absent: stock is rebuilt from movements.
    Entity("medicines", m.Medicine,
           ("name", "description", "sku", "category", "batch_number", "expiry_date", "unit_of_measure",
            "min_threshold", "unit_price", "cost", "pack_size", "loose_unit", "loose_price",
            "received_quantity", "paid_amount", "image_url", "internal_notes"),
           CATALOG, refs=(("supplier", "suppliers", False),), natural=("name", "batch_number")),
    Entity("stock_movements", m.StockMovement,
           ("kind", "delta", "pack_size", "reference", "created_at"),
           APPEND, refs=(("medicine", "medicines", True),), users=("created_by",),
           stamp="created_at", restore=("created_at",)),
    Entity("patients", m.Patient,
           ("mrn", "full_name", "age", "gender", "phone", "address", "allergies", "created_at"),
           CATALOG, natural=("mrn",), restore=("created_at",)),
    Entity("encounters", m.Encounter,
           ("number", "arrival_type", "status", "referral_source", "opened_at", "closed_at", "priority"),
           FORWARD, refs=(("patient", "patients", True),), users=("opened_by",), natural=("number",),
           status_field="status", ranks=("open", "active", "closed"), restore=("opened_at",)),
    Entity("billables", m.BillableItem,
           ("description", "department", "unit_price", "quantity", "paid_amount", "payment_status"),
           FORWARD, direct=False, parent="encounter__",
           refs=(("encounter", "encounters", True), ("service", "services", False)),
           status_field="payment_status", ranks=("AwaitingPayment", "PartialPayment", "PaymentApproved")),
    Entity("payments", m.PaymentTransaction,
           ("receipt_number", "amount", "tender_method", "notes", "created_at", "invoice_number", "processed_by_name",
            "discount_amount", "discount_rule_name", "discount_reason", "forgiven_amount", "vat_amount"),
           APPEND, direct=False, parent="encounter__", refs=(("encounter", "encounters", True),),
           users=("processed_by",), restore=("created_at",)),
    Entity("refunds", m.RefundTransaction,
           ("amount", "reason", "status", "created_at", "processed_by_name", "rule_name"),
           APPEND, direct=False, parent="encounter__", refs=(("encounter", "encounters", True),),
           users=("processed_by",), restore=("created_at",)),
    Entity("orders", m.ClinicalOrder,
           ("order_type", "status", "details", "result_text", "fulfillment", "quantity", "sale_unit", "created_at", "duration_end", "till_medicine_ends",
            "expiry_warning"),
           FORWARD, direct=False, parent="encounter__",
           refs=(("encounter", "encounters", True), ("medicine", "medicines", False), ("billable", "billables", False)),
           users=("created_by",), status_field="status",
           ranks=("Draft", "AwaitingPayment", "PaymentApproved", "InProgress", "Completed", "Reviewed", "Dispensed"),
           restore=("created_at",)),
    Entity("result_values", m.OrderResultValue,
           ("name", "unit", "reference_text", "value", "flag", "sort_order", "created_at"),
           LWW, direct=False, parent="order__encounter__",
           refs=(("order", "orders", True), ("parameter", "lab_parameters", False)),
           users=("entered_by",), restore=("created_at",)),
    Entity("charts", m.DoctorChart,
           ("chief_complaint", "examination", "diagnosis", "clinical_notes", "treatment_plan"),
           LWW, direct=False, parent="encounter__", refs=(("encounter", "encounters", True),),
           natural=("encounter",)),
    Entity("nurse_notes", m.NurseNote,
           ("note_type", "assessment_type", "stage", "content", "vitals", "created_at"),
           LWW, direct=False, parent="encounter__", refs=(("encounter", "encounters", True),),
           users=("created_by",), restore=("created_at",)),
    Entity("appointments", m.Appointment,
           ("scheduled_at", "reason", "kind", "encounter_number", "created_at", "status", "rescheduled_count", "priority"),
           LWW, refs=(("patient", "patients", True),), users=("created_by",), restore=("created_at",)),
    Entity("radiology_reports", m.RadiologyReport,
           ("technique", "findings", "impression", "tag", "attachments"),
           LWW, direct=False, parent="order__encounter__", refs=(("order", "orders", True),),
           users=("reported_by",), natural=("order",)),
    Entity("referrals", m.Referral,
           ("destination_kind", "to_department", "to_branch", "external_institution", "diagnosis",
            "lab_summary", "approval_status", "approved_at", "created_at", "priority", "reception_approved_at",
            "printed_at", "disqualified_reason"),
           LWW, direct=False, parent="encounter__", refs=(("encounter", "encounters", True),),
           users=("approved_by", "created_by"), restore=("created_at",)),
    Entity("equipment_tickets", m.EquipmentTicket,
           ("title", "details", "status", "resolution", "created_at"),
           LWW, users=("created_by",), restore=("created_at",)),
    Entity("damages", m.MedicineDamage,
           ("quantity", "sale_unit", "reason", "created_at"),
           APPEND, refs=(("medicine", "medicines", True),), users=("recorded_by",), restore=("created_at",)),
)

BY_KEY = {e.key: e for e in ENTITIES}


def scope_q(entity: Entity, tin: str, branch: str) -> Q:
    """Rows of this entity that belong to one clinic branch."""
    prefix = "" if entity.direct else entity.parent
    return Q(**{f"{prefix}clinic_tin": tin, f"{prefix}branch_name__iexact": branch})
