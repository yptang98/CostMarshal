from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from costmarshal_v2.actor_runner import (  # noqa: E402
    actor_execution_workspace,
    build_codex_argv,
    isolated_actor_env,
    worktree_changed_paths,
)
from costmarshal_v2.paths import ProjectLayout  # noqa: E402
from costmarshal_v2.routing import default_provider_catalog  # noqa: E402
from costmarshal_v2.state import load_actor, load_project  # noqa: E402


CLI = ROOT / "scripts" / "costmarshal.py"


def cli(temp: Path, *args: str, ok: bool = True) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["COSTMARSHAL_V2_HOME"] = str(temp / "runtime")
    result = subprocess.run([sys.executable, str(CLI), *args], env=env, text=True, capture_output=True)
    if ok and result.returncode:
        raise AssertionError(f"command failed {args}\n{result.stdout}\n{result.stderr}")
    return result


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-v2-actor-security-"))
    try:
        workspace = temp / "workspace"
        workspace.mkdir()
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
        subprocess.run(["git", "-C", str(workspace), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(workspace), "config", "user.name", "CostMarshal Test"], check=True)
        (workspace / "src").mkdir()
        (workspace / "src" / "app.py").write_text("print('baseline')\n", encoding="utf-8")
        (workspace / ".gitignore").write_text("secret.tmp\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(workspace), "add", "."], check=True)
        subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "baseline"], check=True)

        init = json.loads(cli(temp, "init", "--name", "security", "--objective", "actor isolation", "--workspace", str(workspace), "--backend", "local", "--governance", "off", "--allow-unsafe-native-workers").stdout)
        project_dir = Path(init["project"])
        cli(temp, "new-task", "--project", str(project_dir), "--title", "edit", "--purpose", "bounded edit", "--task-type", "small-edit", "--risk", "low", "--allowed-path", "src/app.py", "--claim-path", "src/app.py")
        dispatched = json.loads(cli(temp, "dispatch", "--project", str(project_dir), "--task", "V2-0001", "--unsafe-native").stdout)
        layout = ProjectLayout(root=temp / "runtime", project_dir=project_dir)
        actor = load_actor(layout, dispatched["actor_id"])
        project = load_project(layout)
        execution, sandbox, scopes, base_sha = actor_execution_workspace(layout, project, actor)
        assert execution != workspace.resolve()
        assert sandbox == "workspace-write"
        assert scopes == ("src/app.py",)
        assert base_sha
        assert (execution / "src" / "app.py").is_file()
        (execution / "secret.tmp").write_text("ignored but still detected\n", encoding="utf-8")
        assert "secret.tmp" in worktree_changed_paths(execution, str(base_sha))

        codex_home = temp / "codex-home"
        codex_home.mkdir()
        (codex_home / "longcat.config.toml").write_text("model = 'test'\n", encoding="utf-8")
        (codex_home / "auth.json").write_text("secret-auth\n", encoding="utf-8")
        secrets = temp / "providers.env"
        secrets.write_text("LONGCAT_API_KEY=longcat-secret\nDEEPSEEK_API_KEY=deepseek-secret\n", encoding="utf-8")
        project["secrets_file"] = str(secrets)
        actor["tier"] = "low"
        actor["provider"] = "longcat"
        actor["env_key"] = "LONGCAT_API_KEY"
        actor["profile"] = "longcat"
        with patch.dict(
            os.environ,
            {
                "CODEX_HOME": str(codex_home),
                "OPENAI_API_KEY": "must-not-leak",
                "GH_TOKEN": "must-not-leak",
                "AWS_SECRET_ACCESS_KEY": "must-not-leak",
                "COSTMARSHAL_SECRETS_FILE": str(secrets),
                "LONGCAT_API_KEY": "inherited-selected",
                "DEEPSEEK_API_KEY": "must-not-leak",
            },
            clear=False,
        ):
            env, redactions = isolated_actor_env(project, actor, layout=layout)
        assert env["LONGCAT_API_KEY"] == "inherited-selected"
        for key in ("OPENAI_API_KEY", "GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "DEEPSEEK_API_KEY", "COSTMARSHAL_SECRETS_FILE"):
            assert key not in env
        isolated_home = Path(env["CODEX_HOME"])
        assert isolated_home != codex_home
        assert (isolated_home / "longcat.config.toml").is_file()
        assert not (isolated_home / "auth.json").exists()
        assert "longcat-secret" in redactions and "deepseek-secret" in redactions and "inherited-selected" in redactions

        high_actor = dict(actor)
        high_actor.update({"id": "agent-high-test", "tier": "high", "provider": "codex", "env_key": None, "profile": None})
        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home), "GH_TOKEN": "must-not-leak", "AWS_SECRET_ACCESS_KEY": "must-not-leak"}, clear=False):
            high_env, _ = isolated_actor_env(project, high_actor, layout=layout)
        assert "GH_TOKEN" not in high_env and "AWS_SECRET_ACCESS_KEY" not in high_env
        assert (Path(high_env["CODEX_HOME"]) / "auth.json").is_file()

        report = project_dir / "reports" / "worker.md"
        argv = build_codex_argv(layout, actor, project, report, execution_workspace=execution, sandbox="workspace-write")
        assert str(project_dir) not in [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "--add-dir"]

        bad_root = workspace / "runtime-inside-workspace"
        rejected = subprocess.run(
            [sys.executable, str(CLI), "--root", str(bad_root), "init", "--objective", "reject overlap", "--workspace", str(workspace), "--governance", "off"],
            text=True,
            capture_output=True,
        )
        assert rejected.returncode != 0 and "runtime root must not be inside" in (rejected.stdout + rejected.stderr)
        uncovered = cli(temp, "new-task", "--project", str(project_dir), "--title", "unsafe", "--purpose", "missing claim", "--allowed-path", "src/other.py", ok=False)
        assert uncovered.returncode != 0 and "covered by a write claim" in (uncovered.stdout + uncovered.stderr)
        print("actor security contract ok")
        return 0
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
