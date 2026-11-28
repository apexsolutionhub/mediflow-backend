from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import AuditViewSet, PersonViewSet, UserViewSet

router = DefaultRouter()
# "persons" is registered first so /user/persons/ is not read as a user id.
router.register(r"persons", PersonViewSet, basename="person")
router.register(r"audit", AuditViewSet, basename="audit")
router.register(r"", UserViewSet, basename="user")

urlpatterns = [
    path("", include(router.urls)),
]
