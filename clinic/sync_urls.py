from django.urls import path

from .sync_views import (
    BackupNowView,
    SyncBillingStatusView,
    SyncBillingSubmitView,
    SyncCompleteView,
    SyncConflictResolveView,
    SyncConflictsView,
    SyncPullView,
    SyncPushView,
    SyncRunView,
    SyncSeedView,
    SyncStatusView,
)

urlpatterns = [
    path("push/", SyncPushView.as_view(), name="sync-push"),
    path("pull/", SyncPullView.as_view(), name="sync-pull"),
    path("complete/", SyncCompleteView.as_view(), name="sync-complete"),
    path("billing/status/", SyncBillingStatusView.as_view(), name="sync-billing-status"),
    path("billing/submit/", SyncBillingSubmitView.as_view(), name="sync-billing-submit"),
    path("status/", SyncStatusView.as_view(), name="sync-status"),
    path("conflicts/", SyncConflictsView.as_view(), name="sync-conflicts"),
    path("conflicts/<int:pk>/resolve/", SyncConflictResolveView.as_view(), name="sync-conflict-resolve"),
    path("backup/", BackupNowView.as_view(), name="sync-backup"),
    path("run/", SyncRunView.as_view(), name="sync-run"),
    path("seed/", SyncSeedView.as_view(), name="sync-seed"),
]
