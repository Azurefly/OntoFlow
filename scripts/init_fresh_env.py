"""Prepare one *new* OntoFlow PostgreSQL deployment; never read or migrate SQLite.

Runs on a developer workstation with Python 3.9+ and no third-party packages.
Secrets are generated from the OS cryptographic RNG and saved with mode 0600.
Existing configuration is NEVER overwritten.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import sys


def generate_settings(project_name: str = "ontoflow-fresh", port: int = 8001) -> str:
    if not project_name or not all(c.islower() or c.isdigit() or c in "-_" for c in project_name):
        raise ValueError("Compose 项目名仅允许小写字母、数字、-、_")
    if not (1 <= port <= 65535):
        raise ValueError("端口必须在 1 到 65535 之间")
    values = {
        "APP_ENV": "production",
        "ONTOFLOW_DEMO_MODE": "true",
        "ONTOFLOW_FRESH_ONLY": "1",
        "DATA_DIR": "/app/data",
        "COMPOSE_PROJECT_NAME": project_name,
        "PG_DB": "ontoflow",
        "PG_USER": "ontoflow",
        "PG_PASSWORD": secrets.token_urlsafe(32),
        "BIND_ADDRESS": "127.0.0.1",
        "PORT": str(port),
        "CORS_ORIGINS": "",
        "PG_POOL_SIZE": "8",
        "WORKER_CONCURRENCY": "2",
        "LLM_MAX_CONCURRENCY": "2",
        "EXTRACTION_CONCURRENCY": "2",
        "JOB_MAX_ATTEMPTS": "3",
        "MAX_UPLOAD_MB": "20",
    }
    return ("# OntoFlow v1.3.8 Demo: freshly initialized PostgreSQL only; do not commit this file.\n"
            "# Enter OpenMind account and model endpoint using the Web UI after startup.\n"
            "# Demo accepts HTTP(S) directly: no host whitelist or HTTP opt-in flag is required.\n"
            "# All values below are newly generated; no old installation is read.\n"
            + "\n".join(f"{key}={value}" for key, value in values.items()) + "\n")


def create_env(path: Path, project_name: str = "ontoflow-fresh", port: int = 8001) -> Path:
    payload = generate_settings(project_name, port)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
        os.chmod(path, 0o600)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description="Initialize a NEW OntoFlow PostgreSQL demo deployment")
    ap.add_argument("--output", default=".env", help="path for newly created .env (must not exist)")
    ap.add_argument("--project", default="ontoflow-fresh")
    ap.add_argument("--port", type=int, default=8001)
    args = ap.parse_args(argv)
    try:
        result = create_env(Path(args.output), args.project, args.port)
    except FileExistsError:
        print("检测到已有 .env：已停止，绝不覆盖现有密钥或数据库连接配置。", file=sys.stderr)
        return 2
    except (ValueError, OSError) as exc:
        print(f"初始化失败：{exc}", file=sys.stderr)
        return 2
    print(f"已创建 {result}（0600 权限）；未迁移或读取任何旧数据。")
    print("下一步：docker compose up -d --build")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
