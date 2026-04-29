from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("bots", "0084_recording_file_max_length"),
    ]

    operations = [
        migrations.AlterField(
            model_name="zoomoauthconnection",
            name="is_onbehalf_token_supported",
            field=models.BooleanField(default=True),
        ),
    ]
