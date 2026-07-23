from __future__ import annotations

from unittest import TestCase

from rest_framework import serializers

from bots.serializers import CreateBotSerializer


def _validate(value: dict) -> dict:
    serializer = CreateBotSerializer()
    return serializer.validate_recording_settings(value)


class RecordingQualitySettingsTests(TestCase):
    def test_recording_quality_settings_are_preserved(self) -> None:
        result = _validate(
            {
                "format": "webm",
                "resolution": "1080p",
                "recording_fps": 15,
                "video_bits_per_second": 4_000_000,
                "audio_bits_per_second": 96_000,
            }
        )

        self.assertEqual(result["recording_fps"], 15)
        self.assertEqual(result["video_bits_per_second"], 4_000_000)
        self.assertEqual(result["audio_bits_per_second"], 96_000)

    def test_recording_quality_settings_reject_out_of_range_values(self) -> None:
        invalid_values = [
            ("recording_fps", 4),
            ("recording_fps", 61),
            ("video_bits_per_second", 499_999),
            ("audio_bits_per_second", 320_001),
        ]
        for field, value in invalid_values:
            with self.subTest(field=field, value=value):
                with self.assertRaises(serializers.ValidationError):
                    _validate({field: value})
