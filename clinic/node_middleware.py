"""What a clinic server refuses to do (docs/OFFLINE_SYNC_CONTRACT.md, section 5.1).

A clinic node serves one branch of an existing clinic. It does not host the Apex admin and it
does not register new clinics; both stay on the cloud. Nothing changes on the cloud itself.
"""
from django.conf import settings
from django.http import JsonResponse

BLOCKED_PREFIXES = ("/admin/",)


class ClinicNodeGuardMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if getattr(settings, "NODE_MODE", "cloud") == "clinic" and self._blocked(request):
            return JsonResponse(
                {"detail": "This is not available on a clinic server. Use the cloud for it."}, status=403
            )
        return self.get_response(request)

    @staticmethod
    def _blocked(request) -> bool:
        path = request.path
        if path.startswith(BLOCKED_PREFIXES):
            return True
        # Self-signup of a new clinic: an anonymous POST to the user collection.
        anonymous_signup = (
            path.rstrip("/") == "/api/user"
            and request.method == "POST"
            and not request.META.get("HTTP_AUTHORIZATION")
        )
        return anonymous_signup
