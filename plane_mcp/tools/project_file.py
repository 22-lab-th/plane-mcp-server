"""Project Files, folders, versions, links, storage and lifecycle operations."""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict

from plane_mcp.client import get_plane_client_context
from plane_mcp.extensions import present, project_path, request, segment
from plane_mcp.project_files import upload_path
from plane_mcp.toolkit import Action, build_annotations, build_description, needs

NAME = "project_file"
LEGACY: dict[str, str] = {}
ACTIONS = (
    Action(
        "list",
        ("project_id",),
        ("filters", "cursor", "per_page"),
        read=True,
        note="returns files, folders and breadcrumbs; next_cursor is inside page",
    ),
    Action("retrieve", ("project_id", "file_id"), ("trashed",), read=True),
    Action("get_storage", ("project_id",), read=True),
    Action(
        "list_activity",
        ("project_id",),
        ("file_id", "event", "actor_id", "created_from", "created_to", "cursor", "per_page"),
        read=True,
    ),
    Action("preview_url", ("project_id", "file_id"), ("version_no",), read=True),
    Action("download_url", ("project_id", "file_id"), ("version_no",), read=True),
    Action("create_folder", ("project_id", "name"), ("parent_id",)),
    Action("update_folder", ("project_id", "folder_id"), ("name", "parent_id", "to_root")),
    Action("delete_folder", ("project_id", "folder_id"), ("recursive", "confirm"), destructive=True),
    Action("update", ("project_id", "file_id"), ("name", "folder_id", "to_root", "is_pinned")),
    Action("delete", ("project_id", "file_id"), destructive=True, note="move to trash; recover with restore"),
    Action("restore", ("project_id", "file_id")),
    Action(
        "purge",
        ("project_id", "file_id", "confirm"),
        destructive=True,
        note="confirm=true permanently removes a trashed file and all versions; project admin only",
    ),
    Action("copy", ("project_id", "file_id"), ("name", "folder_id")),
    Action("copy_to_project", ("project_id", "file_id", "target_project_id"), ("name", "folder_id")),
    Action(
        "move_to_project",
        ("project_id", "file_id", "target_project_id", "confirm"),
        ("name", "folder_id"),
        destructive=True,
    ),
    Action("list_versions", ("project_id", "file_id"), ("trashed",), read=True),
    Action("activate_version", ("project_id", "file_id", "version_no")),
    Action("list_entity_links", ("project_id", "entity_type", "entity_id"), read=True),
    Action("link", ("project_id", "file_id", "entity_type", "entity_id")),
    Action("unlink", ("project_id", "file_id", "link_id"), destructive=True),
    Action(
        "initiate_upload",
        ("project_id", "name", "size_bytes", "mime_type"),
        ("folder_id", "file_id", "checksum_sha256", "entity_type", "entity_id"),
    ),
    Action("complete_upload", ("project_id", "file_id", "version_no", "size_bytes"), ("checksum_sha256",)),
    Action("abort_upload", ("project_id", "file_id", "version_no")),
    Action(
        "upload_from_path",
        ("project_id", "file_path"),
        ("name", "mime_type", "folder_id", "file_id", "entity_type", "entity_id"),
        note="reserve, stream PUT and verify; file_id adds a version; local path requires PLANE_FILE_UPLOAD_ROOTS",
    ),
)


class FileFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    folder_id: str | None = None
    q: str | None = None
    mime: str | None = None
    extension: str | None = None
    uploader: str | None = None
    created_from: str | None = None
    created_to: str | None = None
    size_min: int | None = None
    size_max: int | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    pinned: bool | None = None
    trashed: bool | None = None
    ordering: Literal["name", "-name", "size", "-size", "created", "-created", "updated", "-updated"] | None = None


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name=NAME,
        description=build_description(
            "Manage Project Files and their folders, versions and entity links.",
            ACTIONS,
            "Files follow target project access, quota and MIME checks. Use to_root=true to clear a folder/parent. "
            "Upload initiation returns signed URL and exact PUT headers; preserve If-None-Match and Content-Type. "
            "Complete every upload or abort it. A new version does not activate automatically; use activate_version. "
            "preview_url/download_url expire; persist project-file:<id> references in documents instead. "
            "Cross-project operations stay in the same workspace and require access to both projects.",
        ),
        annotations=build_annotations("Project Files", ACTIONS),
    )
    def project_file(
        action: Literal[
            "list",
            "retrieve",
            "get_storage",
            "list_activity",
            "preview_url",
            "download_url",
            "create_folder",
            "update_folder",
            "delete_folder",
            "update",
            "delete",
            "restore",
            "purge",
            "copy",
            "copy_to_project",
            "move_to_project",
            "list_versions",
            "activate_version",
            "list_entity_links",
            "link",
            "unlink",
            "initiate_upload",
            "complete_upload",
            "abort_upload",
            "upload_from_path",
        ],
        project_id: str = "",
        file_id: str = "",
        folder_id: str | None = None,
        parent_id: str | None = None,
        target_project_id: str = "",
        link_id: str = "",
        file_path: str = "",
        name: str | None = None,
        mime_type: str = "",
        size_bytes: int | None = None,
        version_no: int | None = None,
        checksum_sha256: str | None = None,
        entity_type: Literal["issue", "page", "project"] | None = None,
        entity_id: str = "",
        is_pinned: bool | None = None,
        to_root: bool = False,
        recursive: bool = False,
        confirm: bool = False,
        trashed: bool = False,
        filters: FileFilters | None = None,
        cursor: str = "",
        per_page: int = 50,
        event: str = "",
        actor_id: str = "",
        created_from: str = "",
        created_to: str = "",
    ) -> dict[str, Any] | list | str | None:
        values = locals()
        declaration = next(a for a in ACTIONS if a.name == action)
        required = {
            key: str(values[key]) if key == "size_bytes" and values[key] is not None else values[key]
            for key in declaration.requires
        }
        if error := needs(action, **required):
            return error
        if "confirm" in declaration.requires and not confirm or action == "delete_folder" and recursive and not confirm:
            return needs(action, confirm=confirm) or "Error: confirm must be true"
        if (version_no is not None and version_no < 1) or (size_bytes is not None and size_bytes < 0):
            return "Error: version_no must be positive and size_bytes nonnegative"
        if bool(entity_type) != bool(entity_id):
            return "Error: entity_type and entity_id must be provided together"
        if to_root and (parent_id or folder_id):
            return "Error: to_root cannot be combined with a destination folder or parent"
        client, workspace = get_plane_client_context()
        root = project_path(workspace, project_id, "files")
        detail = f"{root}/{segment(file_id)}"
        paging = {"cursor": cursor, "page_size": min(max(per_page, 1), 200)}
        if action == "list":
            query = filters.model_dump(exclude_none=True) if filters else {}
            return request(client, "get", root, params={**query, **paging})
        if action == "get_storage":
            return request(client, "get", f"{root}/storage")
        if action == "list_activity":
            return request(
                client,
                "get",
                f"{root}/activity",
                params={
                    **paging,
                    **present(
                        file_id=file_id or None,
                        action=event or None,
                        actor=actor_id or None,
                        created_from=created_from or None,
                        created_to=created_to or None,
                    ),
                },
            )
        if action == "retrieve":
            return request(client, "get", detail, params={"trashed": str(trashed).lower()})
        if action in {"preview_url", "download_url"}:
            return request(client, "get", f"{detail}/{action.removesuffix('_url')}", params=present(version=version_no))
        if action in {"create_folder", "update_folder", "delete_folder"}:
            endpoint = f"{root}/folders" + (f"/{segment(folder_id)}" if action != "create_folder" else "")
            if action == "delete_folder":
                request_result = request(client, "delete", endpoint, params={"recursive": str(recursive).lower()})
                return request_result or {"folder_id": folder_id, "deleted": True}
            data = present(name=name, parent_id=parent_id)
            if to_root:
                data["parent_id"] = None
            if not data:
                return "Error: update_folder requires name, parent_id or to_root"
            return request(client, "post" if action == "create_folder" else "patch", endpoint, data=data)
        if action == "update":
            data = present(name_display=name, folder_id=folder_id, is_pinned=is_pinned)
            if to_root:
                data["folder_id"] = None
            if not data:
                return "Error: update requires name, folder_id, to_root or is_pinned"
            return request(client, "patch", detail, data=data)
        if action in {"delete", "purge", "unlink"}:
            endpoint = (
                detail
                if action == "delete"
                else f"{detail}/purge"
                if action == "purge"
                else f"{detail}/links/{segment(link_id)}"
            )
            result = request(client, "delete", endpoint, params={"confirm": "true"} if action == "purge" else None)
            return result or {"file_id": file_id, "action": action, "success": True}
        if action == "restore":
            return request(client, "post", f"{detail}/restore", data={})
        if action in {"copy", "copy_to_project", "move_to_project"}:
            suffix = action.replace("_", "-")
            data = present(name_display=name, folder_id=folder_id)
            if action != "copy":
                data["target_project_id"] = target_project_id
            return request(client, "post", f"{detail}/{suffix}", data=data)
        if action == "list_versions":
            return request(client, "get", f"{detail}/versions", params={"trashed": str(trashed).lower()})
        if action == "activate_version":
            return request(client, "post", f"{detail}/versions/{version_no}/activate", data={})
        if action == "list_entity_links":
            return request(client, "get", f"{root}/links", params={"entity_type": entity_type, "entity_id": entity_id})
        if action == "link":
            return request(client, "post", f"{detail}/links", data={"entity_type": entity_type, "entity_id": entity_id})
        link = {"entity_type": entity_type, "entity_id": entity_id} if entity_type else None
        if action == "upload_from_path":
            return upload_path(
                client,
                workspace,
                project_id,
                file_path,
                name=name,
                mime_type=mime_type or None,
                folder_id=folder_id,
                file_id=file_id or None,
                link=link,
            )
        if action == "initiate_upload":
            return request(
                client,
                "post",
                f"{root}/initiate-upload",
                data=present(
                    file_name=name,
                    size_bytes=size_bytes,
                    mime_type=mime_type,
                    folder_id=folder_id,
                    file_id=file_id or None,
                    checksum_sha256=checksum_sha256,
                    link=link,
                ),
            )
        return request(
            client,
            "post",
            f"{detail}/{action.replace('_', '-')}",
            data=present(
                version_no=version_no,
                size_bytes=size_bytes if action == "complete_upload" else None,
                checksum_sha256=checksum_sha256 if action == "complete_upload" else None,
            ),
        )
