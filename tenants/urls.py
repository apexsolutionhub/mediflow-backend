from django.urls import path

from .renewal_views import RenewalStatusView, ResubmitQuarterlyPaymentView
from .resubmit_setup_view import ResubmitSetupPaymentView
from .sales_agents import PublicSalesAgentsView
from .ops_mode_views import OpsModeStatusView, RequestOpsModeChangeView
from .views import BillingMeView, PublicPricingView, SignupRegistrationStatusView, SubmitTenantPaymentView

urlpatterns = [
    path("me/", BillingMeView.as_view(), name="billing-me"),
    path("ops-mode/", OpsModeStatusView.as_view(), name="ops-mode-status"),
    path("ops-mode/request/", RequestOpsModeChangeView.as_view(), name="ops-mode-request"),
    path("sales-agents/", PublicSalesAgentsView.as_view(), name="billing-sales-agents"),
    path("pricing/", PublicPricingView.as_view(), name="billing-pricing"),
    path("submit-payment/", SubmitTenantPaymentView.as_view(), name="billing-submit"),
    path("signup-status/", SignupRegistrationStatusView.as_view(), name="billing-signup-status"),
    path("resubmit-setup/", ResubmitSetupPaymentView.as_view(), name="billing-resubmit-setup"),
    path("renewal-status/", RenewalStatusView.as_view(), name="billing-renewal-status"),
    path("resubmit-quarterly/", ResubmitQuarterlyPaymentView.as_view(), name="billing-resubmit-quarterly"),
]
