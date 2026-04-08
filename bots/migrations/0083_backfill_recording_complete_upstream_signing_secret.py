from django.db import migrations


def backfill_recording_complete_upstream_signing_secret(apps, schema_editor):
    Bot = apps.get_model("bots", "Bot")
    updated_count = 0
    for bot in Bot.objects.iterator(chunk_size=500):
        settings = bot.settings or {}
        callback_settings = settings.get("callback_settings") or {}
        recording_complete = callback_settings.get("recording_complete") or {}
        signing_secret = recording_complete.get("signing_secret")
        upstream_signing_secret = recording_complete.get("upstream_signing_secret")
        if signing_secret and not upstream_signing_secret:
            recording_complete["upstream_signing_secret"] = signing_secret
            callback_settings["recording_complete"] = recording_complete
            settings["callback_settings"] = callback_settings
            bot.settings = settings
            bot.save(update_fields=["settings", "updated_at"])
            updated_count += 1
    print(f"Backfilled recording_complete.upstream_signing_secret for {updated_count} bots")


class Migration(migrations.Migration):
    dependencies = [
        ("bots", "0082_alter_botruntimelease_snapshot_id"),
    ]

    operations = [
        migrations.RunPython(
            backfill_recording_complete_upstream_signing_secret,
            reverse_code=migrations.RunPython.noop,
        ),
    ]

