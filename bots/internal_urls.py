from django.urls import path

from . import internal_views

app_name = "bots_internal"

urlpatterns = [
    path(
        "bot-runtime-leases/<int:lease_id>/complete",
        internal_views.BotRuntimeLeaseCompletionView.as_view(),
        name="bot-runtime-lease-complete",
    ),
]
