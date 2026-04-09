import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=0)
def autopay_charge(self, organization_id):
    """Autopay via Stripe has been removed. This task is a no-op."""
    logger.info("autopay_charge called for organization %s but Stripe billing has been removed", organization_id)


def enqueue_autopay_charge_task(organization):
    """Stripe autopay has been removed. This is a no-op."""
    logger.info("enqueue_autopay_charge_task called for organization %s but Stripe billing has been removed", getattr(organization, "id", organization))
