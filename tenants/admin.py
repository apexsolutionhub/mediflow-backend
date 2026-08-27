from django.contrib import admin

from .billing import approve_quarterly_payment, approve_setup_payment
from .models import TenantAccount, TenantPaymentSubmission


@admin.register(TenantAccount)
class TenantAccountAdmin(admin.ModelAdmin):
    list_display = (
        "clinic_name",
        "clinic_tin",
        "account_status",
        "setup_fee_approved",
        "subscription_paid_until",
        "paid_quarters_count",
    )
    search_fields = ("clinic_name", "clinic_tin")
    list_filter = ("account_status", "setup_fee_approved", "billing_hold")
    actions = ("approve_setup", "approve_quarterly")

    @admin.action(description="Approve setup payment")
    def approve_setup(self, request, queryset):
        for tenant in queryset:
            approve_setup_payment(tenant=tenant, approved_by=request.user)

    @admin.action(description="Approve quarterly payment")
    def approve_quarterly(self, request, queryset):
        for tenant in queryset:
            approve_quarterly_payment(tenant=tenant, approved_by=request.user)


@admin.register(TenantPaymentSubmission)
class TenantPaymentSubmissionAdmin(admin.ModelAdmin):
    list_display = (
        "clinic_tin",
        "payment_kind",
        "amount_etb",
        "status",
        "payment_channel",
        "transaction_ref",
        "submitted_at",
    )
    list_filter = ("status", "payment_kind")
    search_fields = ("clinic_tin", "transaction_ref")
