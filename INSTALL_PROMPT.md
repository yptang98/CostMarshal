# CostMarshal Install Prompt

Paste this into Codex:

```text
Install CostMarshal from https://github.com/yptang98/CostMarshal into my Codex skills directory.

Requirements:
- Clone or download https://github.com/yptang98/CostMarshal.
- Copy the skill folder to $CODEX_HOME/skills/costmarshal, or to ~/.codex/skills/costmarshal if CODEX_HOME is unset.
- Do not copy any local .env files or secrets.
- Verify Python 3.10+ is available with: python --version
- If python is unavailable on Windows, try: py -3 --version
- Do not install WakeWait separately; CostMarshal bundles embedded WakeWait-style wait commands.
- Run: python <installed-skill>/scripts/costmarshal.py init-root
- Run: python <installed-skill>/scripts/costmarshal.py --help
- Tell me I can invoke it with `$costmarshal`, for example: `$costmarshal start a new Arbor project for ...`
- Run skill validation if quick_validate.py is available.
- Report the installed path and validation result.
```
