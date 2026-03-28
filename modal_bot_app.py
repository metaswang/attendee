import logging
import os
from pathlib import Path
import tomllib

from dotenv import load_dotenv

from modal_settings import ModalSettings
import modal

load_dotenv()
ModalSettings.from_env().apply_sdk_auth_env()

logger = logging.getLogger(__name__)

app = modal.App(os.getenv("MODAL__APP_NAME", "attendee-bot-runner"))

APT_PACKAGES = [
    "build-essential",
    "ca-certificates",
    "cmake",
    "curl",
    "gdb",
    "git",
    "gfortran",
    "libcairo2-dev",
    "libopencv-dev",
    "libdbus-1-3",
    "libgbm1",
    "libgl1",
    "libglib2.0-0",
    "libglib2.0-dev",
    "libssl-dev",
    "libx11-dev",
    "libx11-xcb1",
    "libxcb-image0",
    "libxcb-keysyms1",
    "libxcb-randr0",
    "libxcb-shape0",
    "libxcb-shm0",
    "libxcb-xfixes0",
    "libxcb-xtest0",
    "libgl1-mesa-dri",
    "libxfixes3",
    "linux-libc-dev",
    "meson",
    "ninja-build",
    "pkgconf",
    "tar",
    "unzip",
    "zip",
    "vim",
    "libpq-dev",
    "xvfb",
    "x11-xkb-utils",
    "xfonts-100dpi",
    "xfonts-75dpi",
    "xfonts-scalable",
    "x11-apps",
    "libvulkan1",
    "fonts-liberation",
    "xdg-utils",
    "wget",
    "libasound2",
    "libasound2-plugins",
    "alsa-utils",
    "alsa-oss",
    "pulseaudio",
    "pulseaudio-utils",
    "ffmpeg",
    "universal-ctags",
    "xterm",
    "xmlsec1",
    "xclip",
    "libavdevice-dev",
    "gstreamer1.0-alsa",
    "gstreamer1.0-tools",
    "gstreamer1.0-plugins-base",
    "gstreamer1.0-plugins-good",
    "gstreamer1.0-plugins-bad",
    "gstreamer1.0-plugins-ugly",
    "gstreamer1.0-libav",
    "libgstreamer1.0-dev",
    "libgstreamer-plugins-base1.0-dev",
    "libgirepository1.0-dev",
    "python3-gi",
    "gir1.2-gstreamer-1.0",
    "gir1.2-gst-plugins-base-1.0",
]

PYPROJECT_TOML = Path(__file__).with_name("pyproject.toml")
pyproject = tomllib.loads(PYPROJECT_TOML.read_text())
PYTHON_PACKAGES = list(pyproject["project"]["dependencies"])
if "av==12.0.0" not in PYTHON_PACKAGES:
    PYTHON_PACKAGES.append("av==12.0.0")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .env(
        {
            "project": "attendee",
            "cwd": "/attendee",
            "PYTHONPATH": "/attendee",
        }
    )
    .apt_install(APT_PACKAGES)
    .run_commands(
        "wget -q https://mirror.cs.uchicago.edu/google-chrome/pool/main/g/google-chrome-stable/google-chrome-stable_134.0.6998.88-1_amd64.deb",
        'echo "df557edb3d24d8dcaff9557d80733b42afb6626685200d3f34a3b6f528065cad  google-chrome-stable_134.0.6998.88-1_amd64.deb" | sha256sum -c -',
        "apt-get update && apt-get install -y ./google-chrome-stable_134.0.6998.88-1_amd64.deb",
        "rm google-chrome-stable_134.0.6998.88-1_amd64.deb && rm -rf /var/lib/apt/lists/*",
        "wget -q https://storage.googleapis.com/chrome-for-testing-public/134.0.6998.88/linux64/chromedriver-linux64.zip",
        "unzip chromedriver-linux64.zip",
        "mv chromedriver-linux64/chromedriver /usr/local/bin/chromedriver",
        "chmod +x /usr/local/bin/chromedriver",
        "rm -rf chromedriver-linux64 chromedriver-linux64.zip",
    )
    .uv_pip_install(*PYTHON_PACKAGES, extra_options="--no-binary av")
    .add_local_file("entrypoint.sh", "/usr/local/bin/entrypoint.sh", copy=True)
    .add_local_dir(".", remote_path="/attendee", copy=True, ignore=[".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"])
    .workdir("/attendee")
    .run_commands(
        "chmod 0755 /usr/local/bin/entrypoint.sh",
        "mkdir -p /attendee/staticfiles",
        "mkdir -p /etc/opt/chrome/policies/managed",
        "ln -sf /tmp/attendee-chrome-policies.json /etc/opt/chrome/policies/managed/attendee-chrome-policies.json",
    )
    .entrypoint(["/usr/local/bin/entrypoint.sh"])
)

secret_name = os.getenv("MODAL__SECRET_NAME", "attendee-bot-runner-secret")
function_secrets = [modal.Secret.from_name(secret_name)]
modal_bot_cpu = float(os.getenv("MODAL_BOT__CPU", "4"))
modal_bot_max_uptime_seconds = int(os.getenv("MODAL_BOT__MAX_UPTIME_SECONDS", "10800"))
modal_bot_max_containers = int(os.getenv("MODAL_BOT__MAX_CONTAINERS", "50"))


def _mask_env_value(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _modal_env_diagnostics() -> dict:
    expected_vars = [
        "MODAL__TOKEN_ID",
        "MODAL__TOKEN_SECRET",
        "MODAL__APP_NAME",
        "MODAL__SECRET_NAME",
        "MODAL_BOT__CPU",
        "MODAL_BOT__MAX_UPTIME_SECONDS",
        "MODAL_BOT__MAX_CONTAINERS",
        "R2__ENDPOINT",
        "R2__API_BASE_URL",
        "R2__REGION",
        "R2__ACCESS_KEY_ID",
        "R2__SECRET_ACCESS_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_DEFAULT_REGION",
        "AWS_S3_ENDPOINT_URL",
        "DJANGO_SETTINGS_MODULE",
    ]
    present_vars = {}
    for key in expected_vars:
        value = os.getenv(key)
        present_vars[key] = {
            "present": value is not None and value != "",
            "masked_value": _mask_env_value(value),
        }

    return {
        "python_version": os.sys.version,
        "secret_name": secret_name,
        "modal_runtime": {
            "cpu": modal_bot_cpu,
            "max_uptime_seconds": modal_bot_max_uptime_seconds,
            "max_containers": modal_bot_max_containers,
        },
        "env": present_vars,
    }


@app.function(
    image=image,
    secrets=function_secrets,
    cpu=modal_bot_cpu,
    max_containers=modal_bot_max_containers,
    single_use_containers=True,
    timeout=modal_bot_max_uptime_seconds + 3600,
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

    controller = BotController(bot_id)
    try:
        controller.run()
    except Exception as e:
        logger.error(f"Error running bot on Modal: {e}")
        raise
    finally:
        # Final safety cleanup to ensure upload happens even if run() didn't catch a signal or crashed early
        controller.cleanup()


@app.function(
    image=image,
    secrets=function_secrets,
    cpu=0.25,
    max_containers=1,
    single_use_containers=True,
    timeout=600,
    retries=0,
)
def test_modal_env():
    import json

    diagnostics = _modal_env_diagnostics()
    print(json.dumps(diagnostics, indent=2, sort_keys=True))
    return diagnostics
