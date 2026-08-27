from django.urls import path

from .sales_agents import PublicSalesAgentsView
from .views import BillingMeView, PublicPricingView, SignupRegistrationStatusView, SubmitTenantPaymentView

urlpatterns = [
    path("me/", BillingMeView.as_view(), name="billing-me"),
    path("sales-agents/", PublicSalesAgentsView.as_view(), name="billing-sales-agents"),
    path("pricing/", PublicPricingView.as_view(), name="billing-pricing"),
    path("submit-payment/", SubmitTenantPaymentView.as_view(), name="billing-submit"),
    path("signup-status/", SignupRegistrationStatusView.as_view(), name="billing-signup-status"),
]
