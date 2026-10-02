"""Bounded local uploads through Project Files' reserve/PUT/verify lifecycle."""

from __future__ import annotations

import hashlib
import mimetypes
import os
from pathlib import Path
from urllib.parse import urlsplit

import requests

from plane_mcp.attachments import HTTP_TIMEOUT, assert_public_url
from plane_mcp.extensions import project_path, request, segment


def allowed_path(file_path: str) -> Path:
    roots = [
        Path(value).expanduser().resolve()
        for value in os.getenv("PLANE_FILE_UPLOAD_ROOTS", "").split(os.pathsep)
        if value
    ]
    if not roots:
        raise ValueError("Local uploads are disabled; configure PLANE_FILE_UPLOAD_ROOTS")
    path = Path(file_path).expanduser().resolve(strict=True)
    if not path.is_file() or not any(path.is_relative_to(root) for root in roots):
        raise ValueError("File must be a regular file inside PLANE_FILE_UPLOAD_ROOTS")
    limit = int(os.getenv("PLANE_PROJECT_FILE_UPLOAD_MAX_BYTES", str(100 * 1024 * 1024)))
    if not 0 < path.stat().st_size <= limit:
        raise ValueError(f"File must contain 1 to {limit} bytes; server size/quota limits also apply")
    return path


def upload_bytes(client, workspace, project, *, name, payload, mime_type, folder_id=None, file_id=None, link=None):
    """Used by bounded archive importers; verification is always server-side."""
    import io

    return upload_stream(
        client,
        workspace,
        project,
        io.BytesIO(payload),
        name,
        len(payload),
        mime_type,
        hashlib.sha256(payload).hexdigest(),
        folder_id=folder_id,
        file_id=file_id,
        link=link,
    )


def upload_stream(
    client, workspace, project, stream, name, size, mime_type, checksum, *, folder_id=None, file_id=None, link=None
):
    root = project_path(workspace, project, "files")
    created = request(
        client,
        "post",
        f"{root}/initiate-upload",
        data={
            "file_name": name,
            "size_bytes": size,
            "mime_type": mime_type,
            "checksum_sha256": checksum,
            "folder_id": folder_id,
            "file_id": file_id,
            "link": link,
        },
    )
    if not isinstance(created, dict) or created.get("error"):
        return created
    destination = created.get("file", {}).get("id")
    version = created.get("version_no")
    if not destination or version is None:
        return {
            "error": "Plane did not return the file ID and reserved version; inspect Project Files before retrying."
        }
    try:
        upload = created["upload"]
        parsed = urlsplit(upload["url"])
        if parsed.username or parsed.password or parsed.scheme not in {"http", "https"}:
            raise ValueError("Invalid storage upload URL")
        # Permit explicitly configured storage hosts, including local MinIO.
        # No Plane credentials, cookies, or redirect following on this PUT.
        trusted = {urlsplit(client.pages.config.base_path).hostname}
        trusted.update(
            value.strip() for value in os.getenv("PLANE_STORAGE_UPLOAD_HOSTS", "").split(",") if value.strip()
        )
        if parsed.hostname not in trusted:
            assert_public_url(upload["url"])
        headers = upload.get("headers") or {}
        if any(key.lower() in {"authorization", "cookie", "x-api-key", "proxy-authorization"} for key in headers):
            raise ValueError("Unexpected credential header in storage upload")
        response = requests.put(
            upload["url"], data=stream, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=False
        )
        if not 200 <= response.status_code < 300:
            raise ValueError(f"Storage rejected upload ({response.status_code})")
    except (KeyError, TypeError, ValueError, requests.RequestException):
        try:
            request(client, "post", f"{root}/{segment(destination)}/abort-upload", data={"version_no": version})
        except requests.RequestException:
            pass  # Keep the reserved IDs available for explicit recovery.
        # Signed URL/query details must never enter the tool result or logs.
        return {
            "error": "Storage upload failed; its quota reservation was released or is recoverable by abort_upload.",
            "file_id": destination,
            "version_no": version,
        }
    return request(
        client,
        "post",
        f"{root}/{segment(destination)}/complete-upload",
        data={
            "version_no": version,
            "size_bytes": size,
            "checksum_sha256": checksum,
        },
    )


def upload_path(
    client, workspace, project, file_path, *, name=None, mime_type=None, folder_id=None, file_id=None, link=None
):
    path = allowed_path(file_path)
    with path.open("rb") as stream:
        digest = hashlib.sha256()
        size = 0
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
        stream.seek(0)
        return upload_stream(
            client,
            workspace,
            project,
            stream,
            name or path.name,
            size,
            mime_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            digest.hexdigest(),
            folder_id=folder_id,
            file_id=file_id,
            link=link,
        )
