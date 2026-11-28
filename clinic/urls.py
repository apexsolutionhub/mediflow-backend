from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .history import PatientHistoryViewSet
from .treatments import TreatmentAssignmentViewSet, TreatmentTemplateViewSet
from .views import (
    AppointmentViewSet,
    BillableServiceViewSet,
    ChartViewSet,
    ClinicBranchViewSet,
    DashboardView,
    DepartmentViewSet,
    EncounterViewSet,
    MedicineDamageViewSet,
    MedicineViewSet,
    NurseNoteViewSet,
    OrderViewSet,
    OrgManagerPeersView,
    OrgManagerSendView,
    OrgManagerThreadView,
    OrgManagerUnreadView,
    PatientViewSet,
    PaymentRuleViewSet,
    PaymentViewSet,
    ReferralViewSet,
    ReportsView,
    SupplierViewSet,
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
router.register("medicine-damages", MedicineDamageViewSet, basename="medicine-damage")
router.register("suppliers", SupplierViewSet, basename="supplier")
router.register("departments", DepartmentViewSet, basename="department")
router.register("branches", ClinicBranchViewSet, basename="branch")
router.register("tickets", TicketViewSet, basename="ticket")
router.register("referrals", ReferralViewSet, basename="referral")
router.register("payment-rules", PaymentRuleViewSet, basename="payment-rule")
router.register("treatment-assignments", TreatmentAssignmentViewSet, basename="treatment-assignment")
router.register("treatment-templates", TreatmentTemplateViewSet, basename="treatment-template")
router.register("payments", PaymentViewSet, basename="payment")

urlpatterns = [
    path("dashboard/", DashboardView.as_view(), name="clinic-dashboard"),
    path("reports/", ReportsView.as_view(), name="clinic-reports"),
    path("org-chat/peers/", OrgManagerPeersView.as_view(), name="org-chat-peers"),
    path("org-chat/thread/", OrgManagerThreadView.as_view(), name="org-chat-thread"),
    path("org-chat/send/", OrgManagerSendView.as_view(), name="org-chat-send"),
    path("org-chat/unread/", OrgManagerUnreadView.as_view(), name="org-chat-unread"),
    path("patient-history/", PatientHistoryViewSet.as_view({"get": "list"}), name="patient-history"),
    path("patient-history/options/", PatientHistoryViewSet.as_view({"get": "options"}), name="patient-history-options"),
    path("patient-history/<int:pk>/", PatientHistoryViewSet.as_view({"get": "record"}), name="patient-history-detail"),
    path("", include(router.urls)),
]
