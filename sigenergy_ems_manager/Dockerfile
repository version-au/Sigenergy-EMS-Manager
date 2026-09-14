# Explicit FROM (no ARG BUILD_FROM / build.yaml): Supervisor 2026.04.0+
# dropped the old build.yaml/BUILD_FROM build system in favour of plain
# Docker BuildKit, so the base image must be hardcoded here.
#
# Plain python:3.12-alpine on purpose (no s6-overlay/bashio HA base image):
# s6-managed "legacy services" don't reliably pass the container's
# environment (SUPERVISOR_TOKEN, add-on options) through to the wrapped
# process, which silently broke Home Assistant API calls. Running our own
# process directly as PID 1 avoids that whole class of bug.
FROM python:3.12-alpine

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY app/ .

# HA add-on persistent storage lives at /data
RUN mkdir -p /data

CMD ["python3", "main.py"]
