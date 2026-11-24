"""Sync engine: export a clinic branch as plain data, and apply incoming data by merge rule.

Used on both sides. The cloud applies what a clinic node pushes (mode PUSH); the node applies
what the cloud sends back (mode PULL). Every function takes `db`, the database alias to work on,
so the same code runs against the cloud database or a clinic node's database.

Nothing here trusts incoming rows: the scope (`clinic_tin`, `branch_name`) always comes from the
node's credential, never from the payload, and a relation that does not resolve inside the scope
fails the whole apply, which runs in one transaction.
"""
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import ledger, stock
from .models import Medicine, SyncConflict
from .sync_registry import APPEND, BY_KEY, CATALOG, ENTITIES, FORWARD, LWW, scope_q

PUSH, PULL = "push", "pull"
OVERLAP = timedelta(seconds=5)  # delta tokens reach slightly back so nothing slips between runs


class SyncError(Exception):
    """A payload that cannot be applied. The whole apply is rolled back."""


@dataclass
class Scope:
    tin: str
    branch: str


@dataclass
class ApplyResult:
    counts: dict = field(default_factory=dict)
    conflicts: int = 0
    remap: dict = field(default_factory=dict)  # entity key -> {incoming uuid: existing uuid}

    def bump(self, key, what):
        self.counts.setdefault(key, {}).setdefault(what, 0)
        self.counts[key][what] += 1

    def as_dict(self):
        return {"counts": self.counts, "conflicts": self.conflicts, "remap": self.remap}


# ---------- value conversion ----------
def _json(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (Decimal, uuid.UUID)):
        return str(value)
    return value


def _py(model, name, value):
    if value is None:
        return None
    return model._meta.get_field(name).to_python(value)


def _when(value):
    if isinstance(value, datetime):
        return value
    return parse_datetime(value) if value else None


# ---------- export ----------
def ensure_uuids(entity, scope: Scope, db="default"):
    """Rows written by an older backend have no sync_uuid yet; give them one before they travel."""
    qs = entity.model.objects.using(db).filter(scope_q(entity, scope.tin, scope.branch), sync_uuid__isnull=True)
    rows = list(qs)
    for row in rows:
        row.sync_uuid = uuid.uuid4()
    if rows:
        entity.model.objects.using(db).bulk_update(rows, ["sync_uuid"])


def export_scope(scope: Scope, since=None, db="default") -> dict:
    """Every synced row of the branch (or only those changed after `since`), keyed by entity."""
    out = {}
    for ent in ENTITIES:
        ensure_uuids(ent, scope, db)
        qs = ent.model.objects.using(db).filter(scope_q(ent, scope.tin, scope.branch))
        if since is not None:
            qs = qs.filter(**{f"{ent.stamp}__gt": since})
        related = [f for f, _, _ in ent.refs] + list(ent.users)
        if related:
            qs = qs.select_related(*related)
        rows = []
        for obj in qs.order_by("id"):
            rows.append(
                {
                    "sync_uuid": str(obj.sync_uuid),
                    "stamp": _json(getattr(obj, ent.stamp)),
                    "deleted_at": _json(getattr(obj, "deleted_at", None)),
                    "data": {name: _json(getattr(obj, name)) for name in ent.fields},
                    "refs": {
                        name: (str(getattr(obj, name).sync_uuid) if getattr(obj, name) is not None else None)
                        for name, _, _ in ent.refs
                    },
                    "users": {
                        name: (getattr(obj, name).username if getattr(obj, name) is not None else None)
                        for name in ent.users
                    },
                }
            )
        out[ent.key] = rows
    return out


# ---------- apply ----------
class _Apply:
    def __init__(self, scope, mode, db, since_token, local_cutoff):
        self.scope, self.mode, self.db = scope, mode, db
        self.since_token, self.local_cutoff = since_token, local_cutoff
        self.result = ApplyResult()
        self.remap = {}  # incoming uuid str -> existing uuid str (all entities)
        self.users = {}

    def queryset(self, ent):
        return ent.model.objects.using(self.db).filter(scope_q(ent, self.scope.tin, self.scope.branch))

    def user(self, username):
        if not username:
            return None
        if username not in self.users:
            self.users[username] = get_user_model().objects.using(self.db).filter(username=username).first()
        return self.users[username]

    def resolve(self, target_key, raw):
        if raw is None:
            return None
        raw = self.remap.get(str(raw), str(raw))
        return self.queryset(BY_KEY[target_key]).filter(sync_uuid=raw).first()

    def conflict(self, ent, row, existing_data, note):
        self.result.conflicts += 1
        SyncConflict.objects.using(self.db).create(
            clinic_tin=self.scope.tin,
            branch_name=self.scope.branch,
            entity=ent.key,
            row_uuid=row["sync_uuid"],
            label=_label(ent, row["data"]),
            side=SyncConflict.SIDE_CLOUD if self.mode == PUSH else SyncConflict.SIDE_NODE,
            incoming=row["data"],
            existing=existing_data,
            note=note,
        )


def _label(ent, data):
    for name in ("name", "full_name", "number", "mrn", "code", "supplier_name", "title", "receipt_number"):
        if data.get(name):
            return str(data[name])[:255]
    return ent.key


def _existing_data(ent, obj):
    return {name: _json(getattr(obj, name)) for name in ent.fields}


def _differs(ent, obj, data):
    for name in ent.fields:
        if name in data and _py(ent.model, name, data[name]) != getattr(obj, name):
            return True
    return False


def _rank(ent, value):
    try:
        return ent.ranks.index(value)
    except ValueError:
        return 0


def _locally_changed(ctx, ent, obj):
    return (
        ctx.mode == PULL
        and ctx.local_cutoff is not None
        and hasattr(obj, "updated_at")
        and obj.updated_at > ctx.local_cutoff
    )


def _decide(ctx, ent, obj, row):
    """Return "update", "skip" or "conflict" for an incoming row that matches an existing one."""
    data = row["data"]
    if ent.rule == APPEND:
        return "skip"
    incoming_stamp = _when(row.get("stamp"))
    current_stamp = getattr(obj, ent.stamp)
    if ent.rule == FORWARD:
        new, old = _rank(ent, data.get(ent.status_field)), _rank(ent, getattr(obj, ent.status_field))
        if new < old or (new == old and incoming_stamp and incoming_stamp < current_stamp):
            return "skip"
        if not _differs(ent, obj, data):
            return "skip"
        return "conflict" if _locally_changed(ctx, ent, obj) and new == old else "update"
    if ent.rule == LWW:
        if not _differs(ent, obj, data):
            return "skip"
        if incoming_stamp and incoming_stamp <= current_stamp:
            return "skip"
        return "update"
    # catalog: the cloud copy wins unless only the node changed it.
    if not _differs(ent, obj, data):
        return "skip"
    if ctx.mode == PUSH:
        cloud_changed = ctx.since_token is None or current_stamp > ctx.since_token
        return "conflict" if cloud_changed else "update"
    return "conflict" if _locally_changed(ctx, ent, obj) else "update"


def _assign(ctx, ent, obj, row, creating):
    data = row["data"]
    for name in ent.fields:
        if name in data:
            setattr(obj, name, _py(ent.model, name, data[name]))
    for name, target, required in ent.refs:
        raw = row.get("refs", {}).get(name)
        related = ctx.resolve(target, raw)
        if raw is not None and related is None:
            raise SyncError(f"{ent.key}: {name} {raw} is not in this branch.")
        if raw is None and required:
            raise SyncError(f"{ent.key}: {name} is required.")
        setattr(obj, name, related)
    for name in ent.users:
        setattr(obj, name, ctx.user(row.get("users", {}).get(name)))
    if creating and ent.direct:
        obj.clinic_tin, obj.branch_name = ctx.scope.tin, ctx.scope.branch
    if hasattr(obj, "deleted_at") and "deleted_at" in row:
        obj.deleted_at = _when(row["deleted_at"])


def _find_natural(ctx, ent, row):
    if not ent.natural:
        return None
    lookup = {}
    for name in ent.natural:
        if name in row["data"]:
            lookup[name] = _py(ent.model, name, row["data"][name])
        else:
            related = ctx.resolve(dict((f, t) for f, t, _ in ent.refs)[name], row.get("refs", {}).get(name))
            if related is None:
                return None
            lookup[name] = related
    return ctx.queryset(ent).filter(**lookup).first()


def _save(ctx, ent, obj, row):
    obj.save(using=ctx.db)
    fixes = {}
    for name in ent.restore:
        if name in row["data"]:
            fixes[name] = _py(ent.model, name, row["data"][name])
    # On a pull, keep the cloud's change time so the next push does not send these rows back.
    if ctx.mode == PULL and row.get("stamp") and hasattr(obj, "updated_at") and ent.stamp == "updated_at":
        fixes["updated_at"] = _when(row["stamp"])
    if fixes:
        ent.model.objects.using(ctx.db).filter(pk=obj.pk).update(**fixes)


def _apply_movement(ctx, obj):
    """A new stock movement changes the stock by its delta (never by copying a stock number)."""
    medicine = Medicine.objects.using(ctx.db).select_for_update().get(pk=obj.medicine_id)
    ledger.set_stock_units(medicine, ledger.stock_units(medicine) + obj.delta)
    medicine.save(using=ctx.db, update_fields=stock.STOCK_FIELDS)


def _apply_row(ctx, ent, row):
    uid = str(uuid.UUID(str(row["sync_uuid"])))
    row = {**row, "sync_uuid": uid}
    existing = ctx.queryset(ent).filter(sync_uuid=uid).first()
    if existing is None:
        twin = _find_natural(ctx, ent, row)
        if twin is not None and twin.sync_uuid is not None and str(twin.sync_uuid) != uid:
            if ctx.mode == PUSH:
                # The cloud keeps its own id; the node is told to switch to it.
                ctx.remap[uid] = str(twin.sync_uuid)
                ctx.result.remap.setdefault(ent.key, {})[uid] = str(twin.sync_uuid)
            else:
                ent.model.objects.using(ctx.db).filter(pk=twin.pk).update(sync_uuid=uid)
                twin.sync_uuid = uuid.UUID(uid)
            existing = twin
        elif twin is not None:
            existing = twin
    if existing is None:
        obj = ent.model(sync_uuid=uid)
        if ent.model is Medicine:
            obj.on_hand, obj.loose_stock = 0, 0
        _assign(ctx, ent, obj, row, creating=True)
        _save(ctx, ent, obj, row)
        ctx.result.bump(ent.key, "created")
        if ent.key == "stock_movements":
            _apply_movement(ctx, obj)
        return
    verdict = _decide(ctx, ent, existing, row)
    if verdict == "update":
        _assign(ctx, ent, existing, row, creating=False)
        _save(ctx, ent, existing, row)
        ctx.result.bump(ent.key, "updated")
    elif verdict == "conflict":
        ctx.conflict(ent, row, _existing_data(ent, existing), "Changed on both sides since the last sync; the cloud copy was kept.")
        ctx.result.bump(ent.key, "conflicts")
    else:
        ctx.result.bump(ent.key, "skipped")


def apply_scope(scope: Scope, entities: dict, mode: str, db="default", since_token=None, local_cutoff=None) -> ApplyResult:
    """Merge incoming rows into the database. All or nothing: any bad row rolls everything back."""
    ctx = _Apply(scope, mode, db, since_token, local_cutoff)
    with transaction.atomic(using=db):
        for ent in ENTITIES:
            for row in entities.get(ent.key, []):
                try:
                    _apply_row(ctx, ent, row)
                except SyncError:
                    raise
                except (KeyError, ValueError, TypeError) as exc:
                    raise SyncError(f"{ent.key}: bad row ({exc}).") from exc
    return ctx.result


def apply_remap(remap: dict, db="default"):
    """The cloud already had these rows under another id: switch the node's copies to it."""
    for key, pairs in remap.items():
        model = BY_KEY[key].model
        for old, new in pairs.items():
            model.objects.using(db).filter(sync_uuid=old).update(sync_uuid=new)


# ---------- staff and tenant (pulled only: the cloud always wins) ----------
TENANT_FIELDS = (
    "clinic_name", "phone", "logo_url", "branch_name", "account_status", "status_reason",
    "setup_fee_approved", "subscription_payment_approved", "subscription_paid_until",
    "paid_quarters_count", "billing_hold", "billing_started_at", "free_trial_ends_at",
    "modules", "ops_mode", "yearly_fee_etb", "billing_notes", "setup_fee_etb", "quarterly_fee_etb",
)


def export_staff(scope: Scope, db="default") -> list:
    from api.models import UserProfile

    profiles = UserProfile.objects.using(db).filter(
        clinic_tin=scope.tin, branch_name__iexact=scope.branch
    ).select_related("user")
    return [
        {
            "username": p.user.username,
            "password": p.user.password,  # already a hash; never a plain password
            "first_name": p.user.first_name,
            "last_name": p.user.last_name,
            "email": p.user.email,
            "is_active": p.user.is_active,
            "profile": {
                "clinic_name": p.clinic_name,
                "role": p.role,
                "logoUrl": p.logoUrl,
                "branch_name": p.branch_name,
                "is_active": p.is_active,
            },
        }
        for p in profiles
    ]


def apply_staff(scope: Scope, staff: list, db="default") -> int:
    from api.models import UserProfile

    User = get_user_model()
    with transaction.atomic(using=db):
        for item in staff:
            user = User.objects.using(db).filter(username=item["username"]).first()
            if user is None:
                user = User(username=item["username"])
            for name in ("password", "first_name", "last_name", "email", "is_active"):
                setattr(user, name, item[name])
            user.save(using=db)
            profile = UserProfile.objects.using(db).filter(user=user).first() or UserProfile(user=user)
            for name, value in item["profile"].items():
                setattr(profile, name, value)
            profile.clinic_tin = scope.tin
            profile.save(using=db)
    return len(staff)


def export_tenant(tin: str, db="default") -> dict | None:
    from tenants.models import TenantAccount

    tenant = TenantAccount.objects.using(db).filter(clinic_tin=tin).first()
    if tenant is None:
        return None
    snapshot = {name: _json(getattr(tenant, name)) for name in TENANT_FIELDS}
    # A clinic server has no pricing catalog, so it gets the fees the cloud works out for this clinic.
    from tenants.billing import effective_tenant_fees

    snapshot["setup_fee_etb"], snapshot["quarterly_fee_etb"] = effective_tenant_fees(tenant)
    return snapshot


def apply_tenant(tin: str, snapshot: dict | None, db="default"):
    from tenants.models import TenantAccount

    if not snapshot:
        return
    tenant = TenantAccount.objects.using(db).filter(clinic_tin=tin).first() or TenantAccount(clinic_tin=tin)
    for name in TENANT_FIELDS:
        if name in snapshot:
            setattr(tenant, name, _py(TenantAccount, name, snapshot[name]))
    tenant.save(using=db)


def delta_token(now=None) -> str:
    """The cloud time a pull is good up to. Taken before the rows are read, so nothing falls in a gap."""
    return (now or timezone.now()).isoformat()


def delta_since(token):
    """Where the next delta starts: a little before the token, so a row saved mid-run is sent again."""
    since = _when(token)
    return since - OVERLAP if since else None
