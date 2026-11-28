"""Duplicate handling shared by the clinic screens.

Two kinds of answer, both HTTP 409 with a machine-readable `code`:
  * `duplicate_warning`: the save is probably a mistake. The screen shows the message and, if the user insists,
    sends the same request again with `confirm_duplicate: true`.
  * any other code (for example `patient_exists`): the save is refused, with the details the screen needs.
"""

import re

from rest_framework import status
from rest_framework.exceptions import APIException

WARNING_CODE = "duplicate_warning"


class Conflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "conflict"

    def __init__(self, message, code=WARNING_CODE, **extra):
        payload = {"code": code, "detail": message, **extra}
        super().__init__(payload)
        # Keep ids, numbers and nulls as they are; the default error body would turn them all into text.
        self.detail = payload


def confirmed(request) -> bool:
    value = request.data.get("confirm_duplicate") if hasattr(request.data, "get") else None
    return str(value).strip().lower() in {"1", "true", "yes"}


def warn_unless_confirmed(request, message, matches=None):
    """Stop with a warning unless the user already said "save anyway"."""
    if not confirmed(request):
        raise Conflict(message, WARNING_CODE, matches=matches or [])


def text_key(value) -> str:
    """Names that differ only in case or spacing are the same."""
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def digits(value) -> str:
    return re.sub(r"\D+", "", str(value or ""))
