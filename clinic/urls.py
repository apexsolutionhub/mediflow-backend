from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AppointmentViewSet,
    BillableServiceViewSet,
    ChartViewSet,
    DashboardView,
    DepartmentViewSet,
    EncounterViewSet,
    MedicineViewSet,
    NurseNoteViewSet,
    OrderViewSet,
    PatientViewSet,
    PaymentViewSet,
    ReferralViewSet,
    TicketViewSet,
)

router = DefaultRouter()
router.register("patients", PatientViewSet, basename="patient")
router.register("encounters", EncounterViewSet, basename="encounter")
router.register("services", BillableServiceViewSet, basename="service")
router.register("orders", OrderViewSet, basename="order")
router.register("charts", ChartViewSet, basename="chart")
router.register("nurse-notes", NurseNoteViewSet, basename="nurse-note")
router.register("appointments", AppointmentViewSet, basename="appointment")
router.register("medicines", MedicineViewSet, basename="medicine")
router.register("departments", DepartmentViewSet, basename="department")
router.register("tickets", TicketViewSet, basename="ticket")
router.register("referrals", ReferralViewSet, basename="referral")
router.register("payments", PaymentViewSet, basename="payment")

urlpatterns = [
    path("dashboard/", DashboardView.as_view(), name="clinic-dashboard"),
    path("", include(router.urls)),
]
