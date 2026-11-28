"""People working under a role's shared login (see models.StaffPerson).

One username per role and branch; each person has their own password and the password tells them apart at sign-in.
"""

import re

from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import StaffPerson, UserProfile

MIN_PASSWORD = 6
ROLE_LABELS = dict(UserProfile.ROLE_CHOICES)


def name_key(value: str) -> str:
    """Names that differ only in case or spacing are the same person."""
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def clean_name(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def ensure_default_person(profile, name=""):
    """Give a role that has no people yet its default person (the person who owns the role account's password)."""
    if (profile.role or "").lower() == "manager":
        return None
    if profile.persons.exists():
        return profile.persons.filter(is_default=True, is_active=True).first()
    label = clean_name(name) or f"{ROLE_LABELS.get(profile.role, profile.role.title())} (default)"
    return StaffPerson.objects.create(profile=profile, full_name=label, is_default=True)


def password_in_use(profile, raw, exclude=None) -> bool:
    """True when someone in this role already signs in with this password."""
    user = profile.user
    default = profile.persons.filter(is_default=True, is_active=True).first()
    exclude_default = exclude is not None and exclude.is_default
    # The account password belongs to the default person, so it only conflicts when they are not the one editing.
    if not exclude_default and user.has_usable_password() and user.check_password(raw):
        return True
    for person in profile.persons.filter(is_active=True, is_default=False):
        if exclude is not None and person.pk == exclude.pk:
            continue
        if person.password_hash and check_password(raw, person.password_hash):
            return True
    return False


def identify(profile, raw):
    """(matched, person) for a password typed at sign-in. `matched` is False when nobody in the role has it."""
    user = profile.user
    if user.has_usable_password() and user.check_password(raw):
        return True, profile.persons.filter(is_default=True, is_active=True).first()
    for person in profile.persons.filter(is_active=True, is_default=False):
        if person.password_hash and check_password(raw, person.password_hash):
            return True, person
    return False, None


def validate_password(raw, confirm=None):
    raw = raw or ""
    if len(raw) < MIN_PASSWORD:
        raise ValidationError({"password": [f"Password must be at least {MIN_PASSWORD} characters."]})
    if confirm is not None and raw != confirm:
        raise ValidationError({"password": ["Password and confirmation do not match."]})


def create_person(profile, full_name, password, confirm=None):
    name = clean_name(full_name)
    if not name:
        raise ValidationError({"full_name": ["Full name is required."]})
    validate_password(password, confirm)
    ensure_default_person(profile)
    if any(name_key(p.full_name) == name_key(name) for p in profile.persons.filter(is_active=True)):
        raise ValidationError(
            {"full_name": [f"{name} is already registered as {ROLE_LABELS.get(profile.role, profile.role)}."]}
        )
    if password_in_use(profile, password):
        raise ValidationError({"password": ["Someone in this role already uses this password. Choose another."]})
    if not profile.persons.filter(is_default=True, is_active=True).exists():
        # The role has no default person (the last one was removed): this person takes over the account password.
        user = profile.user
        user.set_password(password)
        user.save(update_fields=["password"])
        profile.password_changed_at = timezone.now()
        profile.save(update_fields=["password_changed_at", "updated_at"])
        return StaffPerson.objects.create(profile=profile, full_name=name, is_default=True)
    return StaffPerson.objects.create(profile=profile, full_name=name, password_hash=make_password(password))


def update_person(person, full_name=None, password=None, confirm=None):
    profile = person.profile
    if full_name is not None:
        name = clean_name(full_name)
        if not name:
            raise ValidationError({"full_name": ["Full name is required."]})
        clash = [
            p for p in profile.persons.filter(is_active=True).exclude(pk=person.pk) if name_key(p.full_name) == name_key(name)
        ]
        if clash:
            raise ValidationError({"full_name": [f"{name} is already registered in this role."]})
        person.full_name = name
    if password:
        validate_password(password, confirm)
        if password_in_use(profile, password, exclude=person):
            raise ValidationError({"password": ["Someone in this role already uses this password. Choose another."]})
        if person.is_default:
            user = profile.user
            user.set_password(password)
            user.save(update_fields=["password"])
            profile.password_changed_at = timezone.now()
            profile.save(update_fields=["password_changed_at", "updated_at"])
        else:
            person.password_hash = make_password(password)
    person.save()
    return person


@transaction.atomic
def delete_person(person, promote_to=None):
    """Remove a person. If they were the default, someone else takes the role's password.

    The person row is kept as inactive so the name stays on past records.
    Returns the new default person, or None.
    """
    profile = person.profile
    new_default = None
    if person.is_default:
        others = list(profile.persons.filter(is_active=True, is_default=False).exclude(pk=person.pk))
        if promote_to is not None:
            new_default = next((p for p in others if p.pk == promote_to), None)
            if new_default is None:
                raise ValidationError({"promote_to": ["Choose someone else in this role."]})
        elif others:
            new_default = others[0]
        if new_default is not None:
            user = profile.user
            user.password = new_default.password_hash  # already a hash: their password becomes the account's
            user.save(update_fields=["password"])
            profile.password_changed_at = timezone.now()
            profile.save(update_fields=["password_changed_at", "updated_at"])
            new_default.is_default = True
            new_default.password_hash = ""
            new_default.save()
        else:
            # Nobody left to take the password: lock the login so the removed person can not use it.
            # The username stays; the next person added to this role becomes the default.
            user = profile.user
            user.set_unusable_password()
            user.save(update_fields=["password"])
    person.is_active = False
    person.is_default = False
    person.save()
    return new_default
