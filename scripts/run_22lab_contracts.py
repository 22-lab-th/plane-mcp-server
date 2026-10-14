"""Build a selected Plane checkout and run MCP contracts on disposable Docker data."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plane-dir", type=Path, required=True)
    options = parser.parse_args()
    plane = options.plane_dir.resolve()
    assert (plane / "apps/api/Dockerfile.api").is_file()
    project = "plane-mcp-ci-" + secrets.token_hex(4)
    backend_image, live_image = project + "-backend", project + "-live"
    subprocess.run(
        ["docker", "build", "-t", backend_image, "-f", str(plane / "apps/api/Dockerfile.api"), str(plane / "apps/api")],
        check=True,
    )
    subprocess.run(
        ["docker", "build", "-t", live_image, "-f", str(plane / "apps/live/Dockerfile.live"), str(plane)], check=True
    )
    api_port, storage_port = 18000, 19000
    origin = f"http://127.0.0.1:{api_port}"
    environment = {
        "SECRET_KEY": secrets.token_hex(32),
        "DJANGO_SETTINGS_MODULE": "plane.settings.production",
        "DATABASE_URL": "postgresql://plane:ci-only@db:5432/plane",
        "REDIS_URL": "redis://redis:6379/",
        "RABBITMQ_HOST": "mq",
        "RABBITMQ_USER": "plane",
        "RABBITMQ_PASSWORD": "ci-only",
        "RABBITMQ_VHOST": "plane",
        "AWS_ACCESS_KEY_ID": "ci-access",
        "AWS_SECRET_ACCESS_KEY": "ci-secret-only",
        "AWS_S3_ENDPOINT_URL": "http://minio:9000",
        "AWS_S3_BUCKET_NAME": "uploads",
        "AWS_S3_REGION_NAME": "us-east-1",
        "USE_MINIO": "1",
        "MINIO_PUBLIC_ENDPOINT_URL": f"http://127.0.0.1:{storage_port}",
        "WEB_URL": origin,
        "CORS_ALLOWED_ORIGINS": origin,
        "GUNICORN_WORKERS": "1",
        "LIVE_SERVER_SECRET_KEY": secrets.token_hex(32),
        "PLANE_YJS_REPLACE_URL": "http://live:3001",
        "EMAIL_HOST": "mail-sink.invalid",
        "EMAIL_HOST_USER": "",
        "EMAIL_HOST_PASSWORD": "",
        "ENABLE_TELEMETRY": "0",
    }
    services = {
        "db": {
            "image": "postgres:15.7-alpine",
            "environment": {"POSTGRES_USER": "plane", "POSTGRES_PASSWORD": "ci-only", "POSTGRES_DB": "plane"},
        },
        "redis": {"image": "valkey/valkey:7.2.11-alpine"},
        "mq": {
            "image": "rabbitmq:3.13.6-management-alpine",
            "environment": {
                "RABBITMQ_DEFAULT_USER": "plane",
                "RABBITMQ_DEFAULT_PASS": "ci-only",
                "RABBITMQ_DEFAULT_VHOST": "plane",
            },
        },
        "minio": {
            "image": "minio/minio@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e",
            "command": ["server", "/data"],
            "ports": [f"127.0.0.1:{storage_port}:9000"],
            "environment": {"MINIO_ROOT_USER": "ci-access", "MINIO_ROOT_PASSWORD": "ci-secret-only"},
        },
        "api": {"image": backend_image, "environment": environment, "ports": [f"127.0.0.1:{api_port}:8000"]},
        "worker": {
            "image": backend_image,
            "environment": environment,
            "command": ["./bin/docker-entrypoint-worker.sh"],
        },
        "live": {
            "image": live_image,
            "environment": {
                "API_BASE_URL": "http://api:8000",
                "REDIS_URL": environment["REDIS_URL"],
                "LIVE_SERVER_SECRET_KEY": environment["LIVE_SERVER_SECRET_KEY"],
                "PORT": "3000",
            },
        },
    }
    with tempfile.TemporaryDirectory(prefix=project) as temporary:
        config = Path(temporary) / "compose.json"
        config.write_text(json.dumps({"services": services}))
        config.chmod(0o600)
        compose = ["docker", "compose", "-p", project, "-f", str(config)]
        try:
            subprocess.run([*compose, "up", "-d", "db", "redis", "mq", "minio"], check=True)
            # wait_for_db is bounded separately from migrations, whose failure is decisive.
            subprocess.run(
                [*compose, "run", "--rm", "--no-deps", "--entrypoint", "python", "api", "manage.py", "wait_for_db"],
                check=True,
                timeout=90,
            )
            subprocess.run(
                [
                    *compose,
                    "run",
                    "--rm",
                    "--no-deps",
                    "--entrypoint",
                    "python",
                    "api",
                    "manage.py",
                    "migrate",
                    "--noinput",
                ],
                check=True,
                timeout=180,
            )
            subprocess.run([*compose, "up", "-d", "api", "worker", "live"], check=True)
            for _ in range(90):
                try:
                    if requests.get(origin + "/api/instances/", timeout=2).status_code == 200:
                        break
                except requests.RequestException:
                    pass
                time.sleep(1)
            else:
                raise RuntimeError("Disposable backend did not become ready")
            seed_code = """import django;django.setup()
import json,secrets
from plane.db.models import User,Workspace,WorkspaceMember,Project,ProjectMember,State,APIToken
u=User.objects.create(email="mcp-ci@example.invalid",first_name="MCP CI")
w=Workspace.objects.create(name="MCP CI",slug="mcp-ci",owner=u)
WorkspaceMember.objects.create(workspace=w,member=u,role=20)
projects=[]
for index in range(2):
 p=Project.objects.create(workspace=w,name="MCP validation "+str(index),identifier="MCP"+str(index))
 ProjectMember.objects.create(workspace=w,project=p,member=u,role=20)
 State.objects.create(workspace=w,project=p,name="Backlog",group="backlog",default=True)
 projects.append(str(p.id))
t=APIToken.objects.create(user=u,workspace=w,token=secrets.token_hex(24))
print(json.dumps({"workspace":w.slug,"api_key":t.token,"projects":projects}))
"""
            result = subprocess.run(
                [*compose, "exec", "-T", "api", "python", "-"],
                input=seed_code,
                capture_output=True,
                text=True,
                check=True,
            )
            seed = json.loads(result.stdout)
            env = {
                **os.environ,
                "PLANE_BASE_URL": origin,
                "PLANE_API_KEY": seed["api_key"],
                "PLANE_WORKSPACE_SLUG": seed["workspace"],
            }
            smoke = Path(__file__).with_name("smoke_22lab.py")
            subprocess.run(
                [
                    sys.executable,
                    str(smoke),
                    "--write",
                    "--project-id",
                    seed["projects"][0],
                    "--target-project-id",
                    seed["projects"][1],
                ],
                env=env,
                check=True,
                timeout=600,
            )
        except BaseException:
            subprocess.run([*compose, "logs", "--tail", "50"], check=False)
            raise
        finally:
            # The random project/config above owns every container and anonymous volume removed here.
            subprocess.run([*compose, "down", "-v"], check=True)


if __name__ == "__main__":
    main()
