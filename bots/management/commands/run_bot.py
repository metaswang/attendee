import logging

from django.core.management.base import BaseCommand

from bots.tasks.run_bot_task import run_bot

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Runs the celery task synchronously on a given bot that is already created"

    def add_arguments(self, parser):
        parser.add_argument("--botid", type=int, help="Bot ID")
        parser.add_argument("--lease-id", type=int, help="Bot runtime lease ID")

    def handle(self, *args, **options):
        logger.info("Running run bot task... lease_id=%s bot_id=%s", options.get("lease_id"), options.get("botid"))

        bot_id = options.get("botid")
        lease_id = options.get("lease_id")
        if bot_id is None and lease_id is None:
            raise ValueError("botid or lease-id is required")

        result = run_bot.run(bot_id=bot_id, lease_id=lease_id)

        logger.info(f"Run bot task completed with result: {result}")
