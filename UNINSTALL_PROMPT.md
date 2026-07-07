# CostMarshal Uninstall Prompt

Paste this into Codex:

```text
Uninstall CostMarshal from my Codex skills directory.

Requirements:
- Remove $CODEX_HOME/skills/costmarshal, or ~/.codex/skills/costmarshal if CODEX_HOME is unset.
- Do not delete CostMarshal runtime state unless I explicitly confirm.
- If I confirm runtime cleanup, remove $CODEX_HOME/costmarshal or ~/.codex/costmarshal.
- Do not delete local secret files unless I explicitly confirm.
- Report exactly what was removed and what was preserved.
```

