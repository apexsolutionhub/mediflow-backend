from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path, re_path
from rest_framework_simplejwt.views import TokenRefreshView

from api.views import CustomTokenObtainPairView, health

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", health, name="health"),
    path("api/auth/login/", CustomTokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("api/user/", include("api.urls")),
    path("api/billing/", include("tenants.urls")),
    path("api/clinic/", include("clinic.urls")),
    path("api/sync/", include("clinic.sync_urls")),
]

if getattr(settings, "NODE_MODE", "cloud") == "clinic":
    from clinic.frontend import serve_frontend

    # Last, so every API route above wins: anything else is a page or asset of the web app.
    urlpatterns += [re_path(r"^(?P<path>.*)$", serve_frontend)]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
