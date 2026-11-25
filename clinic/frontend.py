"""Serves the static copy of the web app (built by `npm run build:node`) from a clinic server.

One address for everything: `/api/...` is Django, every other path is a page or asset of the
app, so staff on the LAN open a single URL and no Node.js is needed on the reception PC.
Only wired up when NODE_MODE is "clinic" (see config/urls.py).
"""
import mimetypes
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse, JsonResponse

IMMUTABLE = "public, max-age=31536000, immutable"  # _next/static files carry a content hash in their name
NO_CACHE = "no-cache"


def frontend_root() -> Path:
    return Path(settings.FRONTEND_DIR).resolve()


def resolve_file(url_path: str, root: Path | None = None) -> Path | None:
    """The file for a request path, or None. Never returns anything outside the frontend folder."""
    root = root or frontend_root()
    if not root.is_dir():
        return None
    relative = url_path.strip("/")
    candidates = [root / relative] if relative else [root / "index.html"]
    if relative:
        candidates += [root / relative / "index.html", root / f"{relative}.html"]
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except (OSError, ValueError):
            continue
        if resolved.is_file() and resolved.is_relative_to(root):
            return resolved
    return None


def _file_response(path: Path, status: int = 200, head: bool = False):
    content_type, _ = mimetypes.guess_type(path.name)
    content_type = content_type or "application/octet-stream"
    if head:
        # A HEAD answer carries the headers only. Sending the file body here corrupts the browser's
        # next request on the same connection (net::ERR_INVALID_HTTP_RESPONSE), which stalls page changes.
        response = HttpResponse(status=status, content_type=content_type)
        response["Content-Length"] = str(path.stat().st_size)
    else:
        response = FileResponse(open(path, "rb"), content_type=content_type, status=status)
    immutable = "_next" in path.parts and path.suffix not in {".html", ".txt"}
    response["Cache-Control"] = IMMUTABLE if immutable else NO_CACHE
    return response


def serve_frontend(request, path=""):
    # An unknown /api path is a real 404 for the app, not a page.
    if path.startswith("api/") or path == "api":
        return JsonResponse({"detail": "Not found."}, status=404)
    if request.method not in ("GET", "HEAD"):
        return HttpResponse(status=405)
    root = frontend_root()
    if not root.is_dir():
        raise Http404(
            "The web app is not installed on this clinic server (missing the dist-node folder). "
            "Run `npm run build:node` and copy it next to the backend."
        )
    head = request.method == "HEAD"
    found = resolve_file(path, root)
    if found is not None:
        return _file_response(found, head=head)
    not_found = root / "404.html"
    if not_found.is_file():
        return _file_response(not_found, status=404, head=head)
    raise Http404("Page not found.")
