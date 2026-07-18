# CostMarshal Uninstall Prompt

Paste this into Codex:

```text
Uninstall the CostMarshal Codex plugin.

Requirements:
- Verify the installed selector with `codex plugin list --json`.
- Remove only the plugin with `codex plugin remove costmarshal@costmarshal --json`.
- Leave the `costmarshal` marketplace configured unless I explicitly ask to remove it.
- If a legacy standalone `$costmarshal` Skill exists, do not remove or modify it unless I explicitly ask for legacy cleanup.
- Do not delete CostMarshal runtime state unless I explicitly confirm.
- If I confirm runtime cleanup, remove $CODEX_HOME/costmarshal-v2 or ~/.codex/costmarshal-v2.
- Preserve legacy $CODEX_HOME/costmarshal or ~/.codex/costmarshal unless I explicitly confirm legacy cleanup too.
- Do not delete local secret files unless I explicitly confirm.
- Report exactly what was removed and what was preserved.
```

