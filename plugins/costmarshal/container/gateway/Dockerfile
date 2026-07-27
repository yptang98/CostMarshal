# A production build MUST pass a digest-pinned image, for example:
# --build-arg PYTHON_BASE_IMAGE=python:3.11-slim@sha256:<reviewed-digest>
ARG PYTHON_BASE_IMAGE
FROM ${PYTHON_BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/costmarshal

COPY costmarshal_v2 /opt/costmarshal/costmarshal_v2
COPY scripts/costmarshal_gateway.py /opt/costmarshal/scripts/costmarshal_gateway.py

RUN python -m compileall -q /opt/costmarshal \
    && find /opt/costmarshal -type d -name __pycache__ -prune -exec rm -rf '{}' + \
    && chown -R 65532:65532 /opt/costmarshal

USER 65532:65532

ENTRYPOINT ["python", "/opt/costmarshal/scripts/costmarshal_gateway.py"]
