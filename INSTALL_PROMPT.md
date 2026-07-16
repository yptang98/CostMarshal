# CostMarshal Install Prompt

Paste this into Codex:

```text
Install CostMarshal from https://github.com/yptang98/CostMarshal into my Codex skills directory.

Requirements:
- Clone or download https://github.com/yptang98/CostMarshal.
- Resolve the Codex skills directory: $CODEX_HOME/skills if CODEX_HOME is set, otherwise ~/.codex/skills.
- If costmarshal is not installed, copy the skill folder to <skills-dir>/costmarshal.
- If <skills-dir>/costmarshal already exists, treat this as an update:
  - Read <skills-dir>/costmarshal/VERSION if it exists and report the old version.
  - Move the old installed skill folder to <skills-dir>/costmarshal.backup-<timestamp>.
  - Copy the new CostMarshal skill folder to <skills-dir>/costmarshal.
  - Do not copy .git, .github, __pycache__, .env files, generated `artifacts/`, local runtime folders, or secret files.
  - Preserve $CODEX_HOME/costmarshal-v2 or ~/.codex/costmarshal-v2 runtime state exactly as-is.
  - Preserve legacy $CODEX_HOME/costmarshal or ~/.codex/costmarshal runtime state exactly as-is.
  - Preserve local secret files exactly as-is; do not print secret values.
- Do not copy any local .env files or secrets.
- Verify Python 3.11+ is available with: python --version
- If python is unavailable on Windows, try: py -3.11 --version
- CostMarshal v2 uses scheduler actors and pluggable runtime backends; do not run legacy v1 initialization unless I explicitly ask for it.
- Run: python <installed-skill>/scripts/costmarshal.py --help
- Run: python <installed-skill>/scripts/install_smoke_test.py
- The smoke test must use and remove its own temporary root; do not create a project under the user's persistent CostMarshal runtime.
- Tell me I can invoke it with `$costmarshal`, for example: `$costmarshal start a new Arbor project for ...`
- Run skill validation if quick_validate.py is available.
- Report the installed path, old version if updated, new version, backup path if created, and validation result.
```
