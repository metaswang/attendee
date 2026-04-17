import logging

from celery import shared_task
from celery.exceptions import Retry
from django.db import transaction

from bots.models import Bot, BotEventManager, BotEventSubTypes, BotEventTypes, BotRuntimeLeaseStatuses, BotRuntimeProviderTypes, BotStates
from bots.runtime_providers import VPSDockerRuntimeError, get_runtime_provider
from bots.runtime_scheduler import acquire_lease_lock, release_lease_lock, skip_vps_enabled, write_pending_bot

logger = logging.getLogger(__name__)


@shared_task(bind=True, soft_time_limit=3600, retry_backoff=True, max_retries=8)
def launch_meetbot_runtime(self, bot_id: int):
    logger.info("Launching meetbot runtime for bot %s", bot_id)

    bot = None
    acquired_lock = False
    try:
        with transaction.atomic():
            bot = Bot.objects.select_for_update().select_related("project", "project__organization").get(id=bot_id)
            if bot.state not in {BotStates.JOINING, BotStates.CONNECTING, BotStates.SCHEDULED, BotStates.STAGED}:
                logger.info("Bot %s (%s) is not launchable in state %s", bot_id, bot.object_id, bot.state)
                return None

            if bot.project.organization.out_of_credits():
                logger.error(
                    "Bot %s (%s) was not launched because organization %s has insufficient credits",
                    bot_id,
                    bot.object_id,
                    bot.project.organization.id,
                )
                BotEventManager.create_event(bot=bot, event_type=BotEventTypes.FATAL_ERROR, event_sub_type=BotEventSubTypes.FATAL_ERROR_OUT_OF_CREDITS)
                return None

            existing_lease = getattr(bot, "runtime_lease", None)
            if existing_lease and existing_lease.status in {BotRuntimeLeaseStatuses.PROVISIONING, BotRuntimeLeaseStatuses.ACTIVE}:
                logger.info("Bot %s (%s) already has active/provisioning lease %s", bot_id, bot.object_id, existing_lease.id)
                return existing_lease.id

            acquired_lock = acquire_lease_lock(bot.id)
            if not acquired_lock:
                logger.info("Bot %s (%s) is already waiting on a launch lock, requeueing", bot_id, bot.object_id)
                write_pending_bot(bot, "launch_lock_busy")
                raise self.retry(countdown=15)

            lease = None
            if not skip_vps_enabled():
                try:
                    lease = get_runtime_provider("vps_docker").provision_bot(bot)
                except VPSDockerRuntimeError as exc:
                    logger.info("VPS capacity unavailable for bot %s (%s): %s", bot_id, bot.object_id, exc)
                    lease = None

            if lease is None:
                lease = get_runtime_provider("gcp_compute_instance").provision_bot(bot)
            return lease.id
    except Retry:
        raise
    except Exception as exc:
        try:
            bot = Bot.objects.get(id=bot_id)
            stale_lease = getattr(bot, "runtime_lease", None)
            if (
                stale_lease is not None
                and stale_lease.status == BotRuntimeLeaseStatuses.PROVISIONING
                and bot.first_heartbeat_timestamp is None
            ):
                try:
                    get_runtime_provider(stale_lease.provider).delete_lease(stale_lease)
                    logger.info(
                        "Deleted stale provisioning lease %s for bot %s (%s) after launch failure",
                        stale_lease.id,
                        bot_id,
                        bot.object_id,
                    )
                except Exception:
                    logger.exception(
                        "Failed deleting stale provisioning lease %s for bot %s after launch failure",
                        stale_lease.id,
                        bot_id,
                    )
            write_pending_bot(bot, str(exc))
        except Exception:
            logger.exception("Failed to queue pending bot %s after launch error", bot_id)
        logger.exception("Failed to launch meetbot runtime for bot %s: %s", bot_id, exc)
        raise self.retry(exc=exc, countdown=30)
    finally:
        if acquired_lock:
            release_lease_lock(bot_id)
