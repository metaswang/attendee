"""Default recording quality for meetbot preview / r2_chunks capture.

Used by WebBotAdapter to inject MediaRecorder parameters into chromedriver payloads.
"""

# preview_720p: 1280x720 @ 24fps, ~1.2 Mbps video + 96 kbps audio
RECORDING_FPS = 24
VIDEO_BITS_PER_SECOND = 1_200_000
AUDIO_BITS_PER_SECOND = 96_000
