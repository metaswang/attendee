import logging
import os
import subprocess
import threading

logger = logging.getLogger(__name__)


class ScreenAndAudioRecorder:
    def __init__(self, file_location, recording_dimensions, audio_only, use_websocket_mixed_audio_source=False, websocket_mixed_audio_sample_rate=48000, websocket_mixed_audio_channels=1):
        self.file_location = file_location
        self.ffmpeg_proc = None
        # Screen will have buffer, we will crop to the recording dimensions
        self.screen_dimensions = (recording_dimensions[0] + 10, recording_dimensions[1] + 10)
        self.recording_dimensions = recording_dimensions
        self.audio_only = audio_only
        self.use_websocket_mixed_audio_source = use_websocket_mixed_audio_source
        self.websocket_mixed_audio_sample_rate = websocket_mixed_audio_sample_rate
        self.websocket_mixed_audio_channels = websocket_mixed_audio_channels
        self.paused = False
        self.xterm_proc = None
        self.audio_pipe_read_fd = None
        self.audio_pipe_write_fd = None
        self.audio_pipe_lock = threading.Lock()

    def uses_websocket_mixed_audio_source(self):
        return self.use_websocket_mixed_audio_source

    def _create_audio_input_pipe(self):
        if self.audio_pipe_read_fd is not None or self.audio_pipe_write_fd is not None:
            return
        self.audio_pipe_read_fd, self.audio_pipe_write_fd = os.pipe()

    def _close_audio_pipe_read_fd(self):
        if self.audio_pipe_read_fd is None:
            return
        try:
            os.close(self.audio_pipe_read_fd)
        except OSError:
            pass
        finally:
            self.audio_pipe_read_fd = None

    def _close_audio_pipe_write_fd(self):
        if self.audio_pipe_write_fd is None:
            return
        try:
            os.close(self.audio_pipe_write_fd)
        except OSError:
            pass
        finally:
            self.audio_pipe_write_fd = None

    def start_recording(self, display_var):
        if self.ffmpeg_proc and self.ffmpeg_proc.poll() is None:
            logger.info("start_recording called but FFmpeg is already running — skipping (reconnect scenario)")
            return

        logger.info(f"Starting screen recorder for display {display_var} with dimensions {self.screen_dimensions} and file location {self.file_location}")
        logger.info(f"Audio source: {'websocket mixed audio (PCM pipe) sample_rate={self.websocket_mixed_audio_sample_rate} channels={self.websocket_mixed_audio_channels}' if self.use_websocket_mixed_audio_source else 'ALSA default device (will be silent in virtual/Modal environments without loopback setup)'}")

        pass_fds = ()

        if self.use_websocket_mixed_audio_source:
            self._create_audio_input_pipe()
            audio_input_args = [
                "-thread_queue_size",
                "4096",
                "-f",
                "s16le",
                "-ar",
                str(self.websocket_mixed_audio_sample_rate),
                "-ac",
                str(self.websocket_mixed_audio_channels),
                "-i",
                f"pipe:{self.audio_pipe_read_fd}",
            ]
            pass_fds = (self.audio_pipe_read_fd,)
        else:
            logger.warning("Using ALSA as audio source. In Modal or Docker environments without audio loopback, the recording will have no audio. Ensure record_audio=True in bot settings to use websocket mixed audio.")
            audio_input_args = [
                "-thread_queue_size",
                "4096",
                "-f",
                "alsa",
                "-i",
                "default",
            ]

        if self.audio_only:
            # FFmpeg command for audio-only recording to MP3
            ffmpeg_cmd = [
                "ffmpeg",
                "-y",  # Overwrite output file without asking
                *audio_input_args,
                "-c:a",
                "libmp3lame",  # MP3 codec
                "-b:a",
                "192k",  # Audio bitrate (192 kbps for good quality)
                "-ar",
                str(self.websocket_mixed_audio_sample_rate if self.use_websocket_mixed_audio_source else 44100),
                "-ac",
                str(self.websocket_mixed_audio_channels if self.use_websocket_mixed_audio_source else 1),
                self.file_location,
            ]
        else:
            ffmpeg_cmd = [
                "ffmpeg",
                "-y",
                "-thread_queue_size",
                "256",
                "-framerate",
                "30",
                "-video_size",
                f"{self.screen_dimensions[0]}x{self.screen_dimensions[1]}",
                "-f",
                "x11grab",
                "-draw_mouse",
                "0",
                "-probesize",
                "32",
                "-i",
                display_var,
                *audio_input_args,
                "-vf",
                f"crop={self.recording_dimensions[0]}:{self.recording_dimensions[1]}:10:10",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-g",
                "30",
                "-c:a",
                "aac",
                "-strict",
                "experimental",
                "-b:a",
                "128k",
                self.file_location,
            ]

        logger.info(f"Starting FFmpeg command: {' '.join(ffmpeg_cmd)}")
        self.ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, pass_fds=pass_fds)
        if self.use_websocket_mixed_audio_source:
            self._close_audio_pipe_read_fd()

    def add_mixed_audio_chunk(self, chunk: bytes):
        if not self.use_websocket_mixed_audio_source:
            return
        if self.audio_pipe_write_fd is None:
            logger.warning("[Audio diag] add_mixed_audio_chunk: audio_pipe_write_fd is None, dropping chunk")
            return
        if not chunk:
            return

        if not hasattr(self, "_pipe_write_count"):
            self._pipe_write_count = 0
        self._pipe_write_count += 1
        if self._pipe_write_count <= 3 or self._pipe_write_count % 500 == 0:
            logger.info(
                f"[Audio diag] add_mixed_audio_chunk #{self._pipe_write_count}: "
                f"bytes={len(chunk)} paused={self.paused} "
                f"write_fd={self.audio_pipe_write_fd}"
            )

        chunk_to_write = bytes(len(chunk)) if self.paused else chunk

        with self.audio_pipe_lock:
            if self.audio_pipe_write_fd is None:
                return
            try:
                os.write(self.audio_pipe_write_fd, chunk_to_write)
            except BrokenPipeError:
                logger.warning("[Audio diag] add_mixed_audio_chunk: BrokenPipeError - FFmpeg process may have exited")
                self._close_audio_pipe_write_fd()
            except OSError as e:
                logger.warning(f"[Audio diag] add_mixed_audio_chunk: OSError writing to FFmpeg pipe: {e}")

    # Pauses by muting the audio and showing a black xterm covering the entire screen
    def pause_recording(self):
        if self.paused:
            return True  # Already paused, consider this success

        try:
            sw, sh = self.screen_dimensions

            x, y = 0, 0

            self.xterm_proc = subprocess.Popen(["xterm", "-bg", "black", "-fg", "black", "-geometry", f"{sw}x{sh}+{x}+{y}", "-xrm", "*borderWidth:0", "-xrm", "*scrollBar:false"])

            subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1"], check=True)
            self.paused = True
            return True
        except Exception as e:
            logger.error(f"Failed to pause recording: {e}")
            return False

    # Resumes by unmuting the audio and killing the xterm proc
    def resume_recording(self):
        if not self.paused:
            return True

        try:
            self.xterm_proc.terminate()
            self.xterm_proc.wait()
            self.xterm_proc = None
            subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"], check=True)
            self.paused = False
            return True
        except Exception as e:
            logger.error(f"Failed to resume recording: {e}")
            return False

    def stop_recording(self):
        if not self.ffmpeg_proc:
            return
        self._close_audio_pipe_write_fd()
        self.ffmpeg_proc.terminate()
        self.ffmpeg_proc.wait()
        self.ffmpeg_proc = None
        logger.info(f"Stopped screen and audio recorder for display with dimensions {self.screen_dimensions} and file location {self.file_location}")

    def get_seekable_path(self, path):
        """
        Transform a file path to include '.seekable' before the extension.
        Example: /tmp/file.webm -> /tmp/file.seekable.webm
        """
        base, ext = os.path.splitext(path)
        return f"{base}.seekable{ext}"

    def cleanup(self, skip_seekable=False):
        input_path = self.file_location

        # If no input path at all, then we aren't trying to generate a file at all
        if input_path is None:
            return

        # Check if input file exists
        if not os.path.exists(input_path):
            logger.info(f"Input file does not exist at {input_path}, creating empty file")
            with open(input_path, "wb"):
                pass  # Create empty file
            return

        # if audio only or we should skip seekability, we don't need to make it seekable
        if self.audio_only or skip_seekable:
            if skip_seekable:
                logger.info("skip_seekable is True, skipping seekability to ensure fast upload")
            return

        # if input file is greater than 3 GB, we will skip seekability
        if os.path.getsize(input_path) > 3 * 1024 * 1024 * 1024:
            logger.info("Input file is greater than 3 GB, skipping seekability")
            return

        output_path = self.get_seekable_path(self.file_location)
        # the file is seekable, so we don't need to make it seekable
        try:
            self.make_file_seekable(input_path, output_path)
        except Exception as e:
            logger.error(f"Failed to make file seekable: {e}")
            return

    def make_file_seekable(self, input_path, tempfile_path):
        """Use ffmpeg to move the moov atom to the beginning of the file."""
        logger.info(f"Making file seekable: {input_path} -> {tempfile_path}")
        # log how many bytes are in the file
        logger.info(f"File size: {os.path.getsize(input_path)} bytes")
        command = [
            "ffmpeg",
            "-i",
            str(input_path),  # Input file
            "-c",
            "copy",  # Copy streams without re-encoding
            "-avoid_negative_ts",
            "make_zero",  # Optional: Helps ensure timestamps start at or after 0
            "-movflags",
            "+faststart",  # Optimize for web playback
            "-y",  # Overwrite output file without asking
            str(tempfile_path),  # Output file
        ]

        result = subprocess.run(command, capture_output=True, text=True)

        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg failed to make file seekable: {result.stderr}")

        # Replace the original file with the seekable version
        try:
            os.replace(str(tempfile_path), str(input_path))
            logger.info(f"Replaced original file with seekable version: {input_path}")
        except Exception as e:
            logger.error(f"Failed to replace original file with seekable version: {e}")
            raise RuntimeError(f"Failed to replace original file: {e}")
