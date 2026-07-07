# LongCat Mechanical Task Protocol

## Purpose

Use LongCat as a low-tier mechanical executor when a senior or medium worker has already converted a process into a script, command, runbook, or template. LongCat can change a few explicit parameters and run the known flow. It should not invent new methods or debug freely.

Prefer CostMarshal replay memory for this flow. After the strong agent proves the workflow, the leader should run `costmarshal.py promote-memory --project <project> --source-task <task> --name <memory-name> ...`, then create LongCat replay tasks with `--replay-memory <memory-name>`.

## Eligible Tasks

Assign LongCat when all are true:
- the workflow has already been run successfully by a stronger agent or human
- a complete replay memory file or proven runbook is attached in `allowed_context`
- prior feedback does not show this memory is unsuitable for LongCat on this task type
- the allowed edits are a small whitelist of parameters, paths, thresholds, or output names
- commands are explicit
- success has a clear marker such as an output file, JSON field, exit code, or log line
- failure behavior is "stop and report", not "try broad fixes"

Good examples:
- change `top_k=20` to `top_k=50` and rerun the same evaluation script
- run the same report template on another shard
- update an output path and execute a known command
- fill a completion report from an existing template
- compress a log section after a command completes

Bad examples:
- design a new algorithm
- choose an architecture
- debug unknown test failures
- edit coupled files without a patch plan
- infer missing requirements
- modify secrets, credentials, permissions, or destructive commands

## Required Brief Shape

Use a restrictive brief.

````markdown
# Mechanical Task

Goal:

Allowed edits:
- File: config.yaml
  Keys: top_k, output_path
  Old values:
  New values:

Forbidden edits:
- Any file not listed above
- Any key not listed above

Allowed commands:
```text
python run_eval.py --config config.yaml
```

Success markers:
- `results.json` exists
- log contains `DONE`
- command exit code is 0

Failure protocol:
- Do not modify code to fix failures.
- Write `FAILED`.
- Put the last 80 stderr lines in `failed.md`.
- Set `status.json.state` to `failed` or `escalate`.

Return artifacts:
- status.json
- completion-report.md
- DONE or FAILED or ESCALATE
````

## Leader Checks

Before dispatch:
- verify the runbook or command was previously proven
- verify allowed edits are narrow
- verify no secret values are present
- verify a success marker exists

After completion:
- inspect changed files or generated artifacts
- verify success markers
- record replay memory feedback with `record-memory-feedback`
- sample the output when it will inform a decision
- escalate if LongCat changed anything outside the whitelist

If LongCat fails while following a complete memory file, the leader must decide whether the failure is `memory_issue`, `agent_capability`, `task_mismatch`, or `environment_issue`. Do not assume the memory is bad just because one weak agent failed.

## Escalation Conditions

LongCat must stop and escalate when:
- a command fails
- expected files are missing
- it needs to edit non-whitelisted fields
- it cannot find the file or parameter
- output is ambiguous
- it sees an authentication, permission, quota, or network error
- it would need to reason about correctness beyond the runbook
