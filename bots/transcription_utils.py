import logging
import os
import time
from typing import Any, Callable, Dict, List, Sequence

import requests

from bots.models import Credentials, Recording, TranscriptionFailureReasons, TranscriptionSettings, Utterance
from bots.utils import pcm_to_mp3

logger = logging.getLogger(__name__)


def is_retryable_failure(failure_data):
    return failure_data.get("reason") in [
        TranscriptionFailureReasons.AUDIO_UPLOAD_FAILED,
        TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED,
        TranscriptionFailureReasons.TIMED_OUT,
        TranscriptionFailureReasons.RATE_LIMIT_EXCEEDED,
        TranscriptionFailureReasons.INTERNAL_ERROR,
    ]


def get_empty_transcript_for_utterance_group(utterances):
    # Forms a dict that maps utterance id to empty transcript
    return {utterance.id: {"transcript": "", "words": []} for utterance in utterances}


def get_mp3_for_utterance_group(
    utterances: Sequence[Utterance],
    *,
    silence_seconds: float = 3.0,
    channels: int = 1,
    sample_rate: int,
    sample_width_bytes: int = 2,  # 2 => 16-bit PCM (s16le)
    bitrate_kbps: int = 128,
    io_chunk_bytes: int = 256 * 1024,
) -> bytes:
    if not utterances:
        return b""

    # R2-only runtime no longer concatenates utterance PCM with ffmpeg.
    # Keep the function for compatibility with older call sites and tests.
    return b""


def split_transcription_by_utterance(
    transcription_result: Dict[str, Any],
    utterances: Sequence[Utterance],
    *,
    silence_seconds: float = 3.0,
) -> Dict[int, Dict[str, Any]]:
    """
    Split transcription result from a combined MP3 back into per-utterance results.

    Assumes:
      - utterances were concatenated in THIS order
      - each utterance contributes duration_ms / 1000.0 seconds of audio
      - exactly `silence_seconds` of silence was inserted between utterances

    Returns:
      { utterance_id: {"transcript": str, "words": [...], "language": str|None} }
    """
    if not utterances:
        return {}

    language = transcription_result.get("language")
    words = transcription_result.get("words") or []

    # Build utterance time windows in the combined audio.
    windows: List[tuple[int, float, float]] = []
    t = 0.0
    for u in utterances:
        dur_s = u.duration_ms / 1000.0
        start = t
        end = start + dur_s
        windows.append((u.id, start, end))
        t = end + silence_seconds

    output = {utterance.id: {"transcript": "", "words": [], "language": language} for utterance in utterances}

    # Assign each word to the first window it overlaps with.
    word_index = 0
    for window_index, (utterance_id, start, end) in enumerate(windows):
        utterance_words = []
        next_start = windows[window_index + 1][1] if window_index + 1 < len(windows) else None

        while word_index < len(words):
            w = words[word_index]
            # If word starts at or after window end, stop (no overlap with this window)
            if w["start"] >= end:
                break
            # If word ends after window start, it overlaps
            if w["end"] > start:
                # Check that word doesn't also overlap with next window (unexpected)
                if next_start is not None and w["end"] > next_start:
                    logger.warning(f"Word overlaps with subsequent window, skipping: {w}")
                else:
                    # Create a new word object with the start and end times adjusted to the current window
                    word_adjusted = dict(w)
                    word_adjusted["start"] = word_adjusted["start"] - start
                    word_adjusted["end"] = word_adjusted["end"] - start
                    utterance_words.append(word_adjusted)
            word_index += 1

        output[utterance_id]["words"] = utterance_words
        output[utterance_id]["transcript"] = " ".join(w["word"] for w in utterance_words)

    return output


def get_transcription_via_assemblyai_for_utterance_group(utterances):
    if not utterances:
        return {}, None

    # Keep the public grouped API, but transcribe each utterance independently.
    transcriptions = {}
    for utterance in utterances:
        transcription, error = get_transcription_via_assemblyai_from_mp3(
            retrieve_mp3_data_callback=lambda utterance=utterance: pcm_to_mp3(
                utterance.get_audio_blob().tobytes(),
                sample_rate=utterance.get_sample_rate(),
            ),
            duration_ms=utterance.duration_ms,
            identifier=f"utterance {utterance.id}",
            transcription_settings=utterance.transcription_settings,
            recording=utterance.recording,
        )
        if error:
            return None, error
        transcriptions[utterance.id] = transcription

    return transcriptions, None


def get_transcription_via_assemblyai_from_mp3(
    retrieve_mp3_data_callback: Callable[[], bytes],
    duration_ms: int,
    identifier: str,
    transcription_settings: TranscriptionSettings,
    recording: Recording,
):
    assemblyai_credentials_record = recording.bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.ASSEMBLY_AI).first()
    if not assemblyai_credentials_record:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    assemblyai_credentials = assemblyai_credentials_record.get_credentials()
    if not assemblyai_credentials:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    api_key = assemblyai_credentials.get("api_key")
    if not api_key:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND, "error": "api_key not in credentials"}

    # If the audio blob is less than 175ms in duration, just return an empty transcription
    # Audio clips this short are almost never generated, it almost certainly didn't have any speech
    # and if we send it to the assemblyai api, the upload will fail
    if duration_ms < 175:
        logger.info(f"AssemblyAI transcription skipped for {identifier} because it's less than 175ms in duration")
        return {"transcript": "", "words": []}, None

    headers = {"authorization": api_key}
    base_url = transcription_settings.assemblyai_base_url()

    mp3_data = retrieve_mp3_data_callback()
    upload_response = requests.post(f"{base_url}/upload", headers=headers, data=mp3_data)

    if upload_response.status_code == 401:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_INVALID}

    if upload_response.status_code != 200:
        return None, {"reason": TranscriptionFailureReasons.AUDIO_UPLOAD_FAILED, "status_code": upload_response.status_code, "text": upload_response.text}

    upload_url = upload_response.json()["upload_url"]

    data = {
        "audio_url": upload_url,
        "speech_models": ["universal-3-pro", "universal-2"],
    }

    if transcription_settings.assembly_ai_language_detection():
        data["language_detection"] = True
    elif transcription_settings.assembly_ai_language_code():
        data["language_code"] = transcription_settings.assembly_ai_language_code()

    # Add keyterms_prompt and speech_model if set
    keyterms_prompt = transcription_settings.assemblyai_keyterms_prompt()
    if keyterms_prompt:
        data["keyterms_prompt"] = keyterms_prompt
    speech_model = transcription_settings.assemblyai_speech_model()
    if speech_model:
        data["speech_models"] = [speech_model]
    speech_models = transcription_settings.assemblyai_speech_models()
    if speech_models:
        data["speech_models"] = speech_models

    if transcription_settings.assemblyai_speaker_labels():
        data["speaker_labels"] = True

    language_detection_options = transcription_settings.assemblyai_language_detection_options()
    if language_detection_options:
        data["language_detection_options"] = language_detection_options

    url = f"{base_url}/transcript"
    response = requests.post(url, json=data, headers=headers)

    if response.status_code != 200:
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "status_code": response.status_code, "text": response.text}

    transcript_id = response.json()["id"]
    polling_endpoint = f"{base_url}/transcript/{transcript_id}"

    # Poll the result_url until we get a completed transcription
    max_retries = int(os.getenv("TRANSCRIPTION_POLLING_TIMEOUT_SECONDS", 120))  # Maximum number of retries (2 minutes with 1s sleep)
    retry_count = 0

    while retry_count < max_retries:
        polling_response = requests.get(polling_endpoint, headers=headers)

        if polling_response.status_code != 200:
            logger.error(f"AssemblyAI result fetch failed with status code {polling_response.status_code}")
            time.sleep(10)
            retry_count += 10
            continue

        transcription_result = polling_response.json()

        if transcription_result["status"] == "completed":
            logger.info("AssemblyAI transcription completed successfully, now deleting from AssemblyAI.")

            # Delete the transcript from AssemblyAI
            delete_response = requests.delete(polling_endpoint, headers=headers)
            if delete_response.status_code != 200:
                logger.error(f"AssemblyAI delete failed with status code {delete_response.status_code}: {delete_response.text}")
            else:
                logger.info("AssemblyAI delete successful")

            transcript_text = transcription_result.get("text", "")
            words = transcription_result.get("words", [])

            formatted_words = []
            if words:
                for word in words:
                    formatted_word = {
                        "word": word["text"],
                        "start": word["start"] / 1000.0,
                        "end": word["end"] / 1000.0,
                        "confidence": word["confidence"],
                    }
                    if "speaker" in word:
                        formatted_word["speaker"] = word["speaker"]

                    formatted_words.append(formatted_word)

            transcription = {"transcript": transcript_text, "words": formatted_words, "language": transcription_result.get("language_code", None)}
            return transcription, None

        elif transcription_result["status"] == "error":
            error = transcription_result.get("error")

            if error and "language_detection cannot be performed on files with no spoken audio" in error:
                logger.info(f"AssemblyAI transcription skipped for {identifier} because it did not have any spoken audio and we tried to detect language")
                return {"transcript": "", "words": []}, None

            return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "step": "transcribe_result_poll", "error": error}

        else:  # queued, processing
            logger.info(f"AssemblyAI transcription status: {transcription_result['status']}, waiting...")
            time.sleep(1)
            retry_count += 1

    # If we've reached here, we've timed out
    return None, {"reason": TranscriptionFailureReasons.TIMED_OUT, "step": "transcribe_result_poll"}
