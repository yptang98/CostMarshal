# CostMarshal Codex Plugin Install Prompt

Paste this into Codex. Ordinary users install and operate the plugin from Codex;
the bundled Python scheduler is an internal runtime and diagnostic dependency.

```text
Install the CostMarshal Codex plugin from https://github.com/yptang98/CostMarshal.

Requirements:
- Resolve and record the exact 40-character reviewed Git commit to install. Do not install an unpinned moving branch for a production workspace.
- Use an isolated CODEX_HOME for the pre-install or pre-update smoke.
- For a first install, add the marketplace with:
  codex plugin marketplace add yptang98/CostMarshal --ref <40-character-commit>
- For an existing pinned installation, do not use `marketplace upgrade` to change the pinned commit. Preserve runtime state and replace only the plugin snapshot in this order:
  codex plugin remove costmarshal@costmarshal --json
  codex plugin marketplace remove costmarshal --json
  codex plugin marketplace add yptang98/CostMarshal --ref <new-40-character-commit> --json
  codex plugin add costmarshal@costmarshal --json
- If any update step fails, stop and report it; do not delete or rewrite runtime state and do not silently install a moving source.
- Verify the marketplace with `codex plugin marketplace list` and confirm its name is `costmarshal`.
- On a first install, run `codex plugin add costmarshal@costmarshal`; after either install path, verify `codex plugin list` reports the expected source and version as installed and enabled. Do not add it a second time after the update sequence already succeeded.
- Verify the cached plugin is self-contained: `.codex-plugin/plugin.json`, root `SKILL.md`, `skills/orchestrate-cost-aware-agents/SKILL.md`, `scripts/`, `costmarshal_v2/`, `references/`, `container/`, and `release/` must all exist under the installed plugin root.
- Verify repository-only or secret-bearing material is absent from the plugin cache: no `.git`, `.github`, `artifacts`, `.env`, `secrets.json`, `*.pyc`, or `__pycache__` entry may be copied.
- Verify Python 3.11+ is available for the hidden runtime with `python --version`; on Windows, run `py -3.11 --version` when `python` is unavailable or older.
- Run the repository's plugin validator, Skill validator, contract test, and isolated install smoke against that exact commit.
- In a new Codex task, verify explicit `$orchestrate-cost-aware-agents` discovery and natural-language invocation. Run only read-only `route`, `dashboard`, and `recover` smoke operations in temporary CostMarshal state.
- Preserve `$CODEX_HOME/costmarshal-v2` and legacy `$CODEX_HOME/costmarshal` runtime state exactly across install, update, and plugin removal. Do not copy, print, migrate, or delete secrets.
- If a legacy standalone `$costmarshal` Skill exists, leave its files and runtime state intact but keep it explicit-only; the plugin Skill is the sole implicit entry.
- Report the pinned commit, marketplace snapshot, installed/enabled state, plugin cache path, version, validation results, and temporary-state cleanup.

After installation, tell me to use CostMarshal directly in Codex, for example:
"Use CostMarshal to complete this task with the best cost-quality tradeoff across low, medium, and high APIs."
Do not require me to type Python or CostMarshal CLI commands for normal use.
```
