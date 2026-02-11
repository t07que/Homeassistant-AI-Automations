ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.20
FROM $BUILD_FROM

ENV LANG C.UTF-8

RUN apk add --no-cache python3 py3-pip py3-virtualenv bash \
  && python3 -m venv /opt/venv \
  && /opt/venv/bin/pip install --no-cache-dir --upgrade pip

COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app
COPY app/ /app/
COPY run.sh /app/run.sh
COPY rootfs/ /

ENV PATH="/opt/venv/bin:$PATH"

RUN chmod +x /app/run.sh
RUN chmod +x /etc/services.d/automation_studio/run
