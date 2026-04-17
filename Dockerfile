FROM --platform=linux/amd64 ubuntu:22.04 AS base

SHELL ["/bin/bash", "-c"]

ENV project=attendee
ENV cwd=/$project
ENV UV_PYTHON_INSTALL_DIR="/opt/uv/python"
ENV UV_PROJECT_ENVIRONMENT="/opt/venv"
ENV PATH="/opt/venv/bin:/root/.local/bin:/usr/local/bin:$PATH"

WORKDIR $cwd

ARG DEBIAN_FRONTEND=noninteractive

#  Install Dependencies
RUN set -eux; \
    retry_apt() { \
        local attempt=1 max_attempts=5; \
        while true; do \
            if "$@"; then \
                return 0; \
            fi; \
            if [ "$attempt" -ge "$max_attempts" ]; then \
                return 1; \
            fi; \
            sleep "$((attempt * 10))"; \
            attempt=$((attempt + 1)); \
        done; \
    }; \
    retry_apt apt-get update -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o APT::Update::Error-Mode=any; \
    retry_apt apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    cmake \
    curl \
    gdb \
    git \
    gfortran \
    libcairo2-dev \
    libopencv-dev \
    libdbus-1-3 \
    libgbm1 \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libglib2.0-dev \
    libssl-dev \
    libx11-dev \
    libx11-xcb1 \
    libxcb-image0 \
    libxcb-keysyms1 \
    libxcb-randr0 \
    libxcb-shape0 \
    libxcb-shm0 \
    libxcb-xfixes0 \
    libxcb-xtest0 \
    libgl1-mesa-dri \
    libxfixes3 \
    linux-libc-dev \
    pkgconf \
    meson \
    ninja-build \
    tar \
    unzip \
    zip \
    vim \
    libpq-dev \
    xvfb \
    x11-xkb-utils \
    xfonts-100dpi \
    xfonts-75dpi \
    xfonts-scalable \
    xfonts-cyrillic \
    x11-apps \
    libvulkan1 \
    fonts-liberation \
    xdg-utils \
    wget \
    libasound2 \
    libasound2-plugins \
    alsa \
    alsa-utils \
    alsa-oss \
    pulseaudio \
    pulseaudio-utils \
    ffmpeg \
    universal-ctags \
    xterm \
    xmlsec1 \
    xclip \
    libavdevice-dev \
    gstreamer1.0-alsa \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    libgirepository1.0-dev \
    python3-gi \
    python3.11 \
    python3.11-venv \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 \
    --fix-missing; \
    rm -rf /var/lib/apt/lists/*
# Install a pinned Chrome build directly from chrome-for-testing.
RUN curl -fL --retry 5 --retry-all-errors --connect-timeout 15 \
    -o chrome-linux64.zip \
    https://storage.googleapis.com/chrome-for-testing-public/134.0.6998.88/linux64/chrome-linux64.zip \
    && unzip chrome-linux64.zip \
    && mv chrome-linux64 /opt/chrome-linux64 \
    && ln -sf /opt/chrome-linux64/chrome /usr/local/bin/google-chrome \
    && ln -sf /opt/chrome-linux64/chrome /usr/local/bin/google-chrome-stable \
    && rm -f chrome-linux64.zip

# Install a specific version of ChromeDriver.
RUN wget -q https://storage.googleapis.com/chrome-for-testing-public/134.0.6998.88/linux64/chromedriver-linux64.zip \
    && unzip chromedriver-linux64.zip \
    && mv chromedriver-linux64/chromedriver /usr/local/bin/chromedriver \
    && chmod +x /usr/local/bin/chromedriver \
    && rm -rf chromedriver-linux64 chromedriver-linux64.zip

# Update certificates once after package install
RUN update-ca-certificates

# Alias python3 to python
RUN ln -sf /usr/bin/python3.11 /usr/bin/python

# Install uv (pinned installer; binary copied to PATH for all users)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv

RUN set -eux; \
    retry_apt() { \
        local attempt=1 max_attempts=5; \
        while true; do \
            if "$@"; then \
                return 0; \
            fi; \
            if [ "$attempt" -ge "$max_attempts" ]; then \
                return 1; \
            fi; \
            sleep "$((attempt * 10))"; \
            attempt=$((attempt + 1)); \
        done; \
    }; \
    retry_apt apt-get update -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o APT::Update::Error-Mode=any; \
    retry_apt apt-get install -y --no-install-recommends python3.11-dev; \
    rm -rf /var/lib/apt/lists/*

FROM base AS deps

# Copy dependency files first to leverage Docker cache
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --python /usr/bin/python3.11 --group control --no-dev --no-install-project --no-install-package zoom-meeting-sdk \
    && uv pip install --python /opt/venv/bin/python --no-cache-dir --only-binary zoom-meeting-sdk "zoom-meeting-sdk==0.0.27" \
    && uv pip uninstall --python /opt/venv/bin/python av || true \
    && uv pip install --python /opt/venv/bin/python --no-cache-dir --no-binary av "av==12.0.0"

ENV TINI_VERSION=v0.19.0
ADD https://github.com/krallin/tini/releases/download/${TINI_VERSION}/tini /tini
RUN chmod +x /tini

WORKDIR /opt

FROM deps AS build

# Create non-root user
RUN useradd -m -u 1000 -s /bin/bash app

# Workdir owned by app in one shot during copy
ENV project=attendee
ENV cwd=/$project
WORKDIR $cwd

# Copy only what you need; set ownership at copy time (no --chmod: legacy docker builder compatibility)
COPY --chown=app:app entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh
COPY --chown=app:app . .

# Make STATIC_ROOT writeable for the non-root user so collectstatic can run at startup
RUN mkdir -p "$cwd/staticfiles" && chown -R app:app "$cwd/staticfiles"

# We want the app to be able to dynamically set the chrome policies file.
# However, chrome will load the file from a hardcoded path in a directory that the app cannot write to.
# Therefore, we create a symlink at that path that points to a file in /tmp which the app can write to.
RUN mkdir -p /etc/opt/chrome/policies/managed \
  && ln -s /tmp/attendee-chrome-policies.json /etc/opt/chrome/policies/managed/attendee-chrome-policies.json

# Switch to non-root AFTER copies to avoid permission flakiness
USER app

# Use tini + entrypoint; CMD can be overridden by compose
ENTRYPOINT ["/tini","--","/usr/local/bin/entrypoint.sh"]
CMD ["bash"]
