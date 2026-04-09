import json
import logging
import os

from bots.launch_methods import (
    LAUNCH_METHOD_DOCKER_COMPOSE_MULTI_HOST,
    LAUNCH_METHOD_KUBERNETES,
    current_launch_method,
    uses_hybrid_runtime_scheduler,
)
from bots.models import BotEventManager, BotEventSubTypes, BotEventTypes

logger = logging.getLogger(__name__)


def _meetbot_runtime_scheduler_enabled() -> bool:
    raw = os.getenv("MEETBOT_RUNTIME_SCHEDULER_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def launch_bot(bot):
    # If this instance is running in Kubernetes, use the Kubernetes pod creator
    # which spins up a new pod for the bot
    launch_method = current_launch_method()
    logger.info(f"Launching bot {bot.object_id} ({bot.id}) with method {launch_method}")
    if uses_hybrid_runtime_scheduler():
        if _meetbot_runtime_scheduler_enabled():
            from .tasks.launch_meetbot_runtime_task import launch_meetbot_runtime

            launch_meetbot_runtime.delay(bot.id)
            logger.info("Bot %s (%s) queued for meetbot runtime launch", bot.object_id, bot.id)
            return

        from .runtime_providers import get_runtime_provider

        try:
            provider = get_runtime_provider("gcp_compute_instance")
            lease = provider.provision_bot(bot)
            logger.info(
                "Bot %s (%s) launched via %s lease %s instance %s region=%s",
                bot.object_id,
                bot.id,
                "gcp_compute_instance",
                lease.id,
                lease.provider_instance_id,
                lease.region,
            )
        except Exception as exc:
            logger.error(f"Bot {bot.object_id} ({bot.id}) failed to launch via {launch_method}: {exc}", exc_info=True)
            lease = getattr(bot, "runtime_lease", None)
            if lease is not None and lease.pk is not None:
                lease.mark_failed(str(exc))
            BotEventManager.create_event(
                bot=bot,
                event_type=BotEventTypes.FATAL_ERROR,
                event_sub_type=BotEventSubTypes.FATAL_ERROR_BOT_NOT_LAUNCHED,
                event_metadata={"launch_error": str(exc)},
            )
    elif launch_method == LAUNCH_METHOD_KUBERNETES:
        from .bot_pod_creator import BotPodCreator

        bot_pod_creator = BotPodCreator()
        create_pod_result = bot_pod_creator.create_bot_pod(
            bot_id=bot.id,
            bot=bot,
            bot_name=bot.k8s_pod_name(),
            bot_cpu_request=bot.cpu_request(),
            add_webpage_streamer=bot.should_launch_webpage_streamer(),
            add_persistent_storage=bot.reserve_additional_storage(),
            bot_pod_spec_type=bot.bot_pod_spec_type,
        )
        logger.info(f"Bot {bot.object_id} ({bot.id}) launched via Kubernetes: {create_pod_result}")
        if not create_pod_result.get("created"):
            logger.error(f"Bot {bot.object_id} ({bot.id}) failed to launch via Kubernetes.")
            try:
                BotEventManager.create_event(
                    bot=bot,
                    event_type=BotEventTypes.FATAL_ERROR,
                    event_sub_type=BotEventSubTypes.FATAL_ERROR_BOT_NOT_LAUNCHED,
                    event_metadata={
                        "create_pod_result": json.dumps(create_pod_result),
                    },
                )
            except Exception as e:
                logger.error(f"Failed to create fatal error bot not launched event for bot {bot.object_id} ({bot.id}): {str(e)}")
    elif launch_method == LAUNCH_METHOD_DOCKER_COMPOSE_MULTI_HOST:
        # Launch bot via dedicated Celery app (bot_launcher) which uses ephemeral Docker containers
        from .tasks.run_bot_in_ephemeral_container_task import run_bot_in_ephemeral_container

        # Assign task to specific queue so that it gets picked up by a worker running in a bot launcher VM.
        run_bot_in_ephemeral_container.apply_async(args=[bot.id], queue="bot_launcher_vm")
        logger.info(f"Bot {bot.object_id} ({bot.id}) launched via run_bot_in_ephemeral_container task in queue bot_launcher_vm")
    else:
        from .tasks.run_bot_task import run_bot

        run_bot.delay(bot.id)
