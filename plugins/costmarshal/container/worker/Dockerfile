# Every external build input is intentionally mandatory and reviewed.
ARG NODE_BASE_IMAGE
FROM ${NODE_BASE_IMAGE}

ARG CODEX_NPM_VERSION
ARG CODEX_NPM_INTEGRITY
WORKDIR /opt/codex
COPY package.json package-lock.json ./
RUN CODEX_NPM_VERSION="${CODEX_NPM_VERSION}" \
    CODEX_NPM_INTEGRITY="${CODEX_NPM_INTEGRITY}" \
    node -e 'const lock=require("./package-lock.json"); const declared=require("./package.json").dependencies["@openai/codex"]; const installed=lock.packages["node_modules/@openai/codex"]; if (!process.env.CODEX_NPM_VERSION || declared !== process.env.CODEX_NPM_VERSION) throw new Error("Codex version does not match the reviewed build input"); if (!installed || installed.version !== declared) throw new Error("Codex lockfile version does not match package.json"); if (!process.env.CODEX_NPM_INTEGRITY || installed.integrity !== process.env.CODEX_NPM_INTEGRITY) throw new Error("Codex integrity does not match the reviewed build input");'
RUN npm ci --omit=dev --ignore-scripts \
    && ln -s /opt/codex/node_modules/.bin/codex /usr/local/bin/codex \
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
