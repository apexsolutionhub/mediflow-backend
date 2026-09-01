from datetime import timedelta

from django.utils import timezone

from .billing import TRIAL_DAYS
from .models import TenantAccount
from .pricing import catalog_default_fees, resolve_pricing_for_tenant


def normalize_ops_mode(raw) -> str:
    value = str(raw or "").strip().lower()
    if value in {TenantAccount.OPS_MODE_OFFLINE, "offline_sync", "hybrid"}:
        return TenantAccount.OPS_MODE_OFFLINE
    return TenantAccount.OPS_MODE_ONLINE


def ensure_tenant_account(
    *,
    clinic_tin: str,
    clinic_name: str = "",
    logo_url: str = "",
    branch_name: str = "Main",
    sales_agent=None,
    ops_mode: str = TenantAccount.OPS_MODE_ONLINE,
) -> TenantAccount | None:
    tin = (clinic_tin or "").strip()
    if not tin:
        return None

    fees = catalog_default_fees()
    tenant, created = TenantAccount.objects.get_or_create(
        clinic_tin=tin,
        defaults={
            "clinic_name": (clinic_name or "").strip(),
            "logo_url": (logo_url or "").strip(),
            "branch_name": (branch_name or "Main").strip() or "Main",
            "account_status": TenantAccount.STATUS_ACTIVE,
            "setup_fee_etb": fees["setup_fee_etb"],
            "quarterly_fee_etb": fees["quarterly_fee_etb"],
            "setup_fee_approved": False,
            "subscription_payment_approved": False,
            "provisioned_by_apex": False,
            "sales_agent": sales_agent,
            "ops_mode": normalize_ops_mode(ops_mode),
            "free_trial_ends_at": timezone.now() + timedelta(days=TRIAL_DAYS),
        },
    )
    if not created:
        dirty = False
        name = (clinic_name or "").strip()
        logo = (logo_url or "").strip()
        branch = (branch_name or "").strip()
        mode = normalize_ops_mode(ops_mode)
        if name and tenant.clinic_name != name:
            tenant.clinic_name = name
            dirty = True
        if logo and tenant.logo_url != logo:
            tenant.logo_url = logo
            dirty = True
        if branch and tenant.branch_name != branch:
            tenant.branch_name = branch
            dirty = True
        if mode and tenant.ops_mode != mode:
            tenant.ops_mode = mode
            dirty = True
        if not tenant.fees_manually_set and not tenant.setup_fee_approved:
            catalog = resolve_pricing_for_tenant(tenant)
            setup_fee = int(catalog.get("setup_fee_etb") or fees["setup_fee_etb"])
            quarterly_fee = int(catalog.get("quarterly_fee_etb") or fees["quarterly_fee_etb"])
            if tenant.setup_fee_etb != setup_fee:
                tenant.setup_fee_etb = setup_fee
                dirty = True
            if tenant.quarterly_fee_etb != quarterly_fee:
                tenant.quarterly_fee_etb = quarterly_fee
                dirty = True
        if dirty:
            tenant.save()
    if created:
        from clinic.models import BillableService, Department

        Department.objects.get_or_create(clinic_tin=tin, name="General")
        defaults = [
            ("CONSULT", "Consultation", "Consultation", "consultation", 300, True),
            ("LAB-CBC", "Complete Blood Count", "Laboratory", "lab", 250, False),
            ("RAD-XRAY", "Chest X-ray", "Radiology", "radiology", 400, False),
            ("RX-DISP", "Pharmacy dispensing", "Pharmacy", "pharmacy", 50, False),
        ]
        for code, name, dept, service_type, price, auto_add in defaults:
            BillableService.objects.get_or_create(
                clinic_tin=tin,
                code=code,
                defaults={
                    "name": name,
                    "department": dept,
                    "service_type": service_type,
                    "unit_price": price,
                    "auto_add_on_registration": auto_add,
                },
            )
    return tenant
