import json
import logging
from textwrap import shorten

from django.http import HttpResponseNotAllowed, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from bots.models import BotRuntimeLease
from bots.runtime_providers import get_runtime_provider

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
class BotRuntimeLeaseCompletionView(View):
    http_method_names = ["post"]

    def post(self, request, lease_id: int):
        try:
            lease = BotRuntimeLease.objects.select_related("bot").get(id=lease_id)
        except BotRuntimeLease.DoesNotExist:
            return JsonResponse({"error": "Lease not found"}, status=404)

        auth_header = request.headers.get("Authorization", "")
        expected_auth_header = f"Bearer {lease.shutdown_token}"
        if auth_header != expected_auth_header:
            return JsonResponse({"error": "Unauthorized"}, status=401)

        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON body"}, status=400)

        provider_instance_id = str(payload.get("provider_instance_id") or payload.get("droplet_id") or "").strip()
        if provider_instance_id and lease.provider_instance_id and provider_instance_id != lease.provider_instance_id:
            return JsonResponse({"error": "provider_instance_id does not match lease"}, status=400)

        provider = get_runtime_provider(lease.provider)

        if provider_instance_id and not lease.provider_instance_id:
            lease.provider_instance_id = provider_instance_id
            lease.save(update_fields=["provider_instance_id", "updated_at"])

        try:
            logger.info(
                "Received runtime lease completion for lease=%s bot=%s provider=%s provider_instance_id=%s",
                lease.id,
                lease.bot.object_id,
                lease.provider,
                provider_instance_id or lease.provider_instance_id,
            )
            provider.delete_lease(lease)
        except Exception as exc:
            logger.exception("Failed to delete runtime lease %s for bot %s", lease.id, lease.bot.object_id)
            lease.mark_failed(str(exc))
            return JsonResponse({"error": "Failed to delete runtime instance", "details": str(exc)}, status=502)

        exit_code = payload.get("exit_code")
        final_state = payload.get("final_state")
        reason = payload.get("reason")
        log_tail = (payload.get("log_tail") or "").strip()
        if exit_code not in (None, 0) or final_state == "failed":
            summary_parts = [f"exit_code={exit_code}", f"final_state={final_state}", f"reason={reason}"]
            if log_tail:
                summary_parts.append(f"log_tail={log_tail}")
            lease.last_error = shorten(" | ".join(summary_parts), width=4000, placeholder="...")
            lease.save(update_fields=["last_error", "updated_at"])

        logger.info(
            "Lease %s completion accepted for bot %s with exit_code=%s final_state=%s reason=%s",
            lease.id,
            lease.bot.object_id,
            exit_code,
            final_state,
            reason,
        )
        return JsonResponse({"status": lease.status, "provider_instance_id": lease.provider_instance_id})

    def dispatch(self, request, *args, **kwargs):
        if request.method.lower() != "post":
            return HttpResponseNotAllowed(["POST"])
        return super().dispatch(request, *args, **kwargs)
