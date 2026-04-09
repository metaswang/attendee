import logging
import os
import signal
from datetime import datetime, timezone

from celery import shared_task
from celery.signals import worker_shutting_down

from bots.bot_controller import BotController
from bots.runtime_api_client import BotRuntimeApiClient

logger = logging.getLogger(__name__)


@shared_task(bind=True, soft_time_limit=3600)
def run_bot(self, bot_id=None, lease_id=None):
    logger.info("Running bot task bot_id=%s lease_id=%s", bot_id, lease_id)
    os.environ.setdefault("BOT_RUNTIME_RUN_BOT_ENTERED_AT", datetime.now(timezone.utc).isoformat())
    runtime_api_client = BotRuntimeApiClient.from_environment()
    if lease_id is not None and runtime_api_client is not None:
        bootstrap = runtime_api_client.get_bootstrap()
        bot_controller = BotController(lease_id=lease_id, runtime_bootstrap=bootstrap, runtime_api_client=runtime_api_client)
    else:
        if bot_id is None:
            raise ValueError("bot_id is required when lease bootstrap is not available")
        bot_controller = BotController(bot_id=bot_id)
    bot_controller.run()


def kill_child_processes():
    # Get the process group ID (PGID) of the current process
    pgid = os.getpgid(os.getpid())

    try:
        # Send SIGTERM to all processes in the process group
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # Process group may no longer exist


@worker_shutting_down.connect
def shutting_down_handler(sig, how, exitcode, **kwargs):
    # Just adding this code so we can see how to shut down all the tasks
    # when the main process is terminated.
    # It's likely overkill.
    logger.info("Celery worker shutting down, sending SIGTERM to all child processes")
    kill_child_processes()
