import json
import logging
import os

from django.core.management.base import BaseCommand
from django.utils import timezone

from bots.models import RuntimeCapacityProviders, RuntimeCapacitySnapshot

logger = logging.getLogger(__name__)

try:
    from google.cloud import compute_v1
except ImportError:  # pragma: no cover - environments without the dependency
    compute_v1 = None


class Command(BaseCommand):
    help = "Sync cached GCP runtime capacity snapshots from Compute Engine regional quota data."

    def handle(self, *args, **options):
        if compute_v1 is None:
            raise RuntimeError("google-cloud-compute is not installed")

        project_id = os.getenv("GCP_PROJECT_ID")
        if not project_id:
            raise RuntimeError("GCP_PROJECT_ID is not set")

        regions = [item.strip() for item in os.getenv("GCP_BOT_REGIONS", "").split(",") if item.strip()]
        if not regions:
            logger.info("No GCP_BOT_REGIONS configured; skipping runtime capacity sync")
            return

        soft_caps = json.loads(os.getenv("GCP_BOT_REGION_SOFT_CAPS_JSON", "{}") or "{}")
        metric_name = os.getenv("GCP_BOT_QUOTA_METRIC", "CPUS")
        client = compute_v1.RegionsClient()

        for region in regions:
            region_resource = client.get(project=project_id, region=region)
            quota = next((item for item in region_resource.quotas if item.metric == metric_name), None)
            if quota is None:
                logger.warning("Region %s does not expose quota metric %s; skipping", region, metric_name)
                continue

            quota_limit = int(quota.limit or 0)
            quota_usage = int(quota.usage or 0)
            soft_cap = soft_caps.get(region)
            effective_limit = min(quota_limit, int(soft_cap)) if soft_cap is not None else quota_limit
            effective_available = max(0, effective_limit - quota_usage)

            snapshot, _ = RuntimeCapacitySnapshot.objects.update_or_create(
                provider=RuntimeCapacityProviders.GCP_COMPUTE_INSTANCE,
                region=region,
                defaults={
                    "quota_limit": quota_limit,
                    "quota_usage": quota_usage,
                    "soft_cap": soft_cap,
                    "effective_available": effective_available,
                    "last_synced_at": timezone.now(),
                    "metadata": {
                        "metric": metric_name,
                        "status": region_resource.status,
                        "zones": list(region_resource.zones),
                    },
                },
            )
            logger.info(
                "Synced GCP runtime capacity region=%s quota_limit=%s quota_usage=%s soft_cap=%s effective_available=%s snapshot_id=%s",
                region,
                quota_limit,
                quota_usage,
                soft_cap,
                effective_available,
                snapshot.id,
            )
