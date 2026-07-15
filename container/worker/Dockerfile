# syntax=docker/dockerfile:1.7
# Both inputs are intentionally mandatory: callers must review and pin them.
ARG NODE_BASE_IMAGE
FROM ${NODE_BASE_IMAGE}

ARG CODEX_NPM_VERSION
RUN test -n "${CODEX_NPM_VERSION}" \
    && npm install --global "@openai/codex@${CODEX_NPM_VERSION}" \
    && npm cache clean --force

COPY costmarshal-worker.js /usr/local/bin/costmarshal-worker
COPY costmarshal-isolation-canary.js /usr/local/bin/costmarshal-isolation-canary
COPY costmarshal-escape-probe.js /usr/local/bin/costmarshal-escape-probe
RUN chmod 0555 /usr/local/bin/costmarshal-worker /usr/local/bin/costmarshal-isolation-canary /usr/local/bin/costmarshal-escape-probe \
    && mkdir -p /home/worker/.codex /bootstrap /out /workspace \
    && chown -R 65532:65532 /home/worker /out

USER 65532:65532
WORKDIR /workspace
ENTRYPOINT []
