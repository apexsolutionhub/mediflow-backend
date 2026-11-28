"""Audit trail: who did what.

`record()` writes one AuditEvent. It never raises, so a logging problem can not break the action being logged.
Clinic viewsets call `record_from_view()` after every successful change (see clinic.views.TenantScopedMixin).
"""

import logging

from .models import AuditEvent, StaffPerson

logger = logging.getLogger(__name__)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SECRET_WORDS = ("password", "token", "secret", "passphrase")
MAX_VALUE = 200
MAX_FIELDS = 40


def _person_from_request(request):
    token = getattr(request, "auth", None)
    try:
        person_id = token.get("person_id") if token is not None else None
    except Exception:
        person_id = None
    if not person_id:
        return None
    return StaffPerson.objects.filter(pk=person_id).first()


def _clean(value):
    if isinstance(value, (dict, list, tuple)):
        text = str(value)
    else:
        text = "" if value is None else str(value)
    return text if len(text) <= MAX_VALUE else text[: MAX_VALUE - 1] + "…"


def sanitize(data):
    """A short, secret-free copy of a request body to keep with the event."""
    if not hasattr(data, "items"):
        return {}
    out = {}
    for key, value in data.items():
        if len(out) >= MAX_FIELDS:
            break
        name = str(key)
        if any(word in name.lower() for word in SECRET_WORDS):
            continue
        out[name] = _clean(value)
    return out


def record(
    request=None,
    *,
    action,
    entity="",
    entity_id="",
    summary="",
    details=None,
    user=None,
    person=None,
):
    try:
        user = user or getattr(request, "user", None)
        if user is not None and not getattr(user, "is_authenticated", False):
            user = None
        profile = getattr(user, "profile", None) if user is not None else None
        if person is None and request is not None:
            person = _person_from_request(request)
        if person is None and profile is not None and (profile.role or "") != "manager":
            person = profile.persons.filter(is_default=True, is_active=True).first()
        name = person.full_name if person is not None else (getattr(user, "username", "") or "")
        return AuditEvent.objects.create(
            clinic_tin=(getattr(profile, "clinic_tin", "") or ""),
            branch_name=(getattr(profile, "branch_name", "") or ""),
            actor_user=user,
            actor_person=person,
            actor_name=name[:255],
            actor_username=(getattr(user, "username", "") or "")[:150],
            actor_role=(getattr(profile, "role", "") or "")[:32],
            action=action[:40],
            entity=entity[:80],
            entity_id=str(entity_id or "")[:64],
            summary=(summary or "")[:500],
            details=details or {},
        )
    except Exception:  # pragma: no cover - logging must never break the request
        logger.exception("Could not write an audit event")
        return None


def _entity_name(view):
    try:
        model = view.get_serializer_class().Meta.model
        return str(model._meta.verbose_name)
    except Exception:
        return str(getattr(view, "basename", None) or view.__class__.__name__.replace("ViewSet", "").lower())


def record_from_view(view, request, response):
    """Log a successful change made through a DRF view."""
    if request.method in SAFE_METHODS or response.status_code >= 400:
        return None
    action_name = getattr(view, "action", None) or request.method.lower()
    kind = {"POST": "create", "PUT": "update", "PATCH": "update", "DELETE": "delete"}.get(request.method, "other")
    if action_name not in {"create", "update", "partial_update", "destroy", "post", "put", "patch", "delete"}:
        kind = action_name  # a custom action: approve, refund, dispense...
    entity = _entity_name(view)
    entity_id = view.kwargs.get("pk") or ""
    data = getattr(response, "data", None)
    if not entity_id and isinstance(data, dict):
        entity_id = data.get("id") or ""
    label = f"{entity} #{entity_id}" if entity_id else entity
    verb = {"create": "Created", "update": "Changed", "delete": "Deleted"}.get(kind, kind.replace("_", " ").capitalize())
    return record(
        request,
        action=kind,
        entity=entity,
        entity_id=entity_id,
        summary=f"{verb} {label}",
        details={"method": request.method, "path": request.path, "fields": sanitize(request.data)},
    )
