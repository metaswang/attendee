import json
import logging
import os

from django.core.management.base import BaseCommand, CommandError

from bots.bots_api_utils import BotCreationSource, create_bot
from bots.launch_bot_utils import launch_bot
from bots.models import Project

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Create and launch a bot through the normal API path while using LAUNCH_BOT_METHOD=modal."

    def add_arguments(self, parser):
        parser.add_argument("--project-object-id", type=str, default=os.getenv("MODAL_EXPERIMENT__PROJECT_OBJECT_ID"), help="Project object id to create the bot under.")
        parser.add_argument("--meeting-url", type=str, help="Meeting URL to join.")
        parser.add_argument("--bot-name", type=str, default="Modal Recorder")
        parser.add_argument("--recording-upload-uri", type=str, help="Full output uri, e.g. r2://bucket/path/file.mp4")
        parser.add_argument("--recording-format", type=str, default=os.getenv("MODAL_BOT__RECORDING_FORMAT", "mp4"))
        parser.add_argument("--max-uptime-seconds", type=int, default=int(os.getenv("MODAL_BOT__MAX_UPTIME_SECONDS", "10800")))
        parser.add_argument("--metadata-json", type=str, default="{}")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        if os.getenv("LAUNCH_BOT_METHOD") != "modal":
            raise CommandError("Set LAUNCH_BOT_METHOD=modal before running this experiment.")

        project_object_id = options["project_object_id"]
        if not project_object_id:
            raise CommandError("Missing --project-object-id or MODAL_EXPERIMENT__PROJECT_OBJECT_ID.")

        meeting_url = options["meeting_url"]
        if not meeting_url:
            raise CommandError("Missing --meeting-url. Provide the live meeting URL before running the experiment.")

        project = Project.objects.filter(object_id=project_object_id).first()
        if not project:
            raise CommandError(f"Project {project_object_id} not found.")

        try:
            metadata = json.loads(options["metadata_json"])
        except json.JSONDecodeError as exc:
            raise CommandError(f"Invalid --metadata-json: {exc}") from exc

        payload = {
            "meeting_url": meeting_url,
            "bot_name": options["bot_name"],
            "metadata": metadata,
            "recording_settings": {
                "format": options["recording_format"],
            },
            "automatic_leave_settings": {
                "max_uptime_seconds": options["max_uptime_seconds"],
            },
        }
        if options["recording_upload_uri"]:
            payload["external_media_storage_settings"] = {
                "recording_upload_uri": options["recording_upload_uri"],
            }

        self.stdout.write(json.dumps(payload, indent=2))
        if options["dry_run"]:
            self.stdout.write("Dry run complete. No bot created.")
            return

        bot, error = create_bot(payload, BotCreationSource.API, project)
        if error:
            raise CommandError(json.dumps(error))

        launch_bot(bot)
        self.stdout.write(f"Created bot {bot.object_id} and dispatched launch via modal.")
