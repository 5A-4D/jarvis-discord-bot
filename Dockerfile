FROM python:3.13-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential git \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install --requirement /tmp/requirements.txt

FROM python:3.13-slim-bookworm AS runtime

ARG HOST_UID=1000
ARG HOST_GID=1000
ARG HOST_HOME=/home/jarvis

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=${HOST_HOME} \
    PATH=/opt/venv/bin:${HOST_HOME}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    XDG_RUNTIME_DIR=/run/user/${HOST_UID}

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        file \
        git \
        jq \
        less \
        libgomp1 \
        openssh-client \
        passwd \
        procps \
        ripgrep \
        sqlite3 \
        systemd \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid "${HOST_GID}" jarvis \
    && useradd --uid "${HOST_UID}" --gid "${HOST_GID}" --home-dir "${HOST_HOME}" --no-create-home --shell /bin/bash jarvis \
    && install -d -o "${HOST_UID}" -g "${HOST_GID}" /opt/jarvis "/run/user/${HOST_UID}"

COPY --from=builder /opt/venv /opt/venv
COPY --chown=${HOST_UID}:${HOST_GID} . /opt/jarvis

WORKDIR /opt/jarvis
USER ${HOST_UID}:${HOST_GID}

CMD ["python", "main.py"]
