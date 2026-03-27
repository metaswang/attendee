import os

import modal

app = modal.App(os.getenv("MODAL__APP_NAME", "attendee-bot-runner"))

image = modal.Image.from_registry(
    os.getenv("MODAL__IMAGE"),
    add_python="3.11",
)

secret_name = os.getenv("MODAL__SECRET_NAME")
function_secrets = [modal.Secret.from_name(secret_name)] if secret_name else []


@app.function(
    image=image,
    secrets=function_secrets,
    timeout=int(os.getenv("MODAL_BOT__MAX_UPTIME_SECONDS", "10800")) + 3600,
    retries=0,
)
def run_bot_on_modal(
    bot_id: int,
    bot_name: str | None = None,
    meeting_url: str | None = None,
    recording_upload_uri: str | None = None,
    other_params: dict | None = None,
):
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", os.getenv("DJANGO_SETTINGS_MODULE", "attendee.settings.production"))

    import django

    django.setup()

    from bots.bot_controller import BotController
    from bots.modal_launcher import apply_modal_runtime_overrides

    apply_modal_runtime_overrides(
        bot_id,
        bot_name=bot_name,
        meeting_url=meeting_url,
        recording_upload_uri=recording_upload_uri,
        other_params=other_params,
    )

    BotController(bot_id).run()
