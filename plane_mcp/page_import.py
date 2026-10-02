"""Import Markdown documents and their images into Plane project Pages."""

from __future__ import annotations

import html
import os
import stat
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from markdown_it import MarkdownIt
from plane.errors import HttpError

from plane_mcp.tools.page import (
    _fetch_public_image,
    _normalise_image,
    _project_page_endpoint,
    _upload_asset,
)

MARKDOWN_SUFFIXES = {".md", ".markdown"}
MAX_BUNDLE_BYTES = 50 * 1024 * 1024
MAX_EXTRACTED_BYTES = 100 * 1024 * 1024
MAX_BUNDLE_ENTRIES = 500


@dataclass
class ImportPageResult:
    source: str
    name: str
    page_id: str | None = None
    parent_id: str | None = None
    assets_uploaded: int = 0
    warnings: list[str] = field(default_factory=list)


def _allowed_roots() -> list[Path]:
    return [
        Path(value).expanduser().resolve()
        for value in os.getenv("PLANE_FILE_UPLOAD_ROOTS", "").split(os.pathsep)
        if value
    ]


def _allowed_import_path(file_path: str) -> Path:
    roots = _allowed_roots()
    if not roots:
        raise ValueError("Local imports are disabled; configure PLANE_FILE_UPLOAD_ROOTS first")
    path = Path(file_path).expanduser().resolve(strict=True)
    if not any(path.is_relative_to(root) for root in roots):
        raise ValueError(f"Import path is outside PLANE_FILE_UPLOAD_ROOTS: {file_path!r}")
    return path


def _safe_extract(bundle: Path, target: Path) -> None:
    if bundle.stat().st_size > MAX_BUNDLE_BYTES:
        raise ValueError(f"ZIP bundle exceeds the {MAX_BUNDLE_BYTES // 1024 // 1024} MB limit")
    try:
        archive = zipfile.ZipFile(bundle)
    except zipfile.BadZipFile as exc:
        raise ValueError("Invalid ZIP bundle") from exc

    with archive:
        entries = archive.infolist()
        if len({str(PurePosixPath(info.filename)) for info in entries}) != len(entries):
            raise ValueError("ZIP contains duplicate file paths")
        if len(entries) > MAX_BUNDLE_ENTRIES:
            raise ValueError(f"ZIP bundle contains more than {MAX_BUNDLE_ENTRIES} entries")
        if sum(info.file_size for info in entries) > MAX_EXTRACTED_BYTES:
            raise ValueError(f"ZIP expands beyond the {MAX_EXTRACTED_BYTES // 1024 // 1024} MB limit")

        for info in entries:
            member = PurePosixPath(info.filename)
            if member.is_absolute() or ".." in member.parts:
                raise ValueError(f"Unsafe ZIP path: {info.filename!r}")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError(f"ZIP symlinks are not allowed: {info.filename!r}")
            destination = (target / Path(*member.parts)).resolve()
            if not destination.is_relative_to(target.resolve()):
                raise ValueError(f"Unsafe ZIP path: {info.filename!r}")
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, destination.open("wb") as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)


def _markdown_files(root: Path) -> list[Path]:
    if root.is_file():
        if root.suffix.lower() not in MARKDOWN_SUFFIXES:
            raise ValueError("Import source must be Markdown, a directory, or a ZIP bundle")
        return [root]
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in MARKDOWN_SUFFIXES
        and not any(part.startswith(".") for part in path.relative_to(root).parts)
    )
    if not files:
        raise ValueError("No Markdown files were found")
    return files


def _page_name(path: Path) -> str:
    return (path.stem.strip() or "Imported page")[:255]


def _image_component_for_external_url(url: str) -> str:
    escaped = html.escape(url, quote=True)
    return f'<image-component src="{escaped}" alignment="center" status="uploaded"></image-component>'


def _read_import_image(path: Path, import_root: Path) -> tuple[bytes, str, str]:
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(import_root.resolve()):
        raise ValueError("Image path escapes the import root")
    if not resolved.is_file():
        raise ValueError("Image path is not a regular file")
    payload = resolved.read_bytes()
    filename, content_type = _normalise_image(payload, resolved.name, "")
    return payload, filename, content_type


def _render_markdown_with_assets(
    *,
    client,
    workspace_slug: str,
    project_id: str,
    page_id: str,
    markdown_path: Path,
    import_root: Path,
    remote_images: Literal["copy", "keep"],
) -> tuple[str, int, list[str]]:
    markdown = markdown_path.read_text(encoding="utf-8-sig")
    parser = MarkdownIt("commonmark", {"html": False, "linkify": True})
    default_image = parser.renderer.rules.get("image")
    cache: dict[str, str] = {}
    warnings: list[str] = []
    uploaded_count = 0

    def render_image(tokens, index, options, env):
        nonlocal uploaded_count
        token = tokens[index]
        source = token.attrGet("src") or ""
        if source in cache:
            return cache[source]
        try:
            if source.startswith(("https://", "http://")):
                if remote_images == "keep":
                    rendered = _image_component_for_external_url(source)
                else:
                    payload, filename, content_type = _fetch_public_image(source, "")
                    rendered = _upload_asset(
                        client, workspace_slug, project_id, page_id, payload, filename, content_type
                    )["image_html"]
                    uploaded_count += 1
            elif source.startswith(("data:", "file:")):
                raise ValueError("data: and file: image URLs are not supported")
            else:
                candidate = (markdown_path.parent / source).resolve(strict=True)
                payload, filename, content_type = _read_import_image(candidate, import_root)
                rendered = _upload_asset(client, workspace_slug, project_id, page_id, payload, filename, content_type)[
                    "image_html"
                ]
                uploaded_count += 1
            cache[source] = rendered
            return rendered
        except (OSError, ValueError, HttpError) as exc:
            warnings.append(f"{source}: {exc}")
            escaped = html.escape(source)
            return f"<p><em>Image not imported: {escaped}</em></p>"

    parser.renderer.rules["image"] = render_image
    try:
        rendered = parser.render(markdown)
    finally:
        if default_image is not None:
            parser.renderer.rules["image"] = default_image
    return rendered or "<p></p>", uploaded_count, warnings


def _create_folder(client, workspace_slug: str, project_id: str, name: str, parent_id: str | None, access: int):
    response = client.pages._post(
        _project_page_endpoint(workspace_slug, project_id),
        {"name": name[:255], "node_type": "folder", "parent": parent_id, "access": access},
    )
    return str(response["id"])


def _archive_page(client, workspace_slug: str, project_id: str, page_id: str) -> None:
    client.pages._post(f"{_project_page_endpoint(workspace_slug, project_id, page_id)}/archive", {})


def import_markdown_path(
    *,
    client,
    workspace_slug: str,
    project_id: str,
    file_path: str,
    parent_id: str | None = None,
    access: int = 0,
    remote_images: Literal["copy", "keep"] = "copy",
    on_error: Literal["stop", "continue"] = "stop",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Import one Markdown file, a directory, or a ZIP bundle into project Pages."""
    if remote_images not in {"copy", "keep"}:
        raise ValueError("remote_images must be 'copy' or 'keep'")
    if on_error not in {"stop", "continue"}:
        raise ValueError("on_error must be 'stop' or 'continue'")
    source = _allowed_import_path(file_path)

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if source.is_file() and source.suffix.lower() == ".zip":
        temp_dir = tempfile.TemporaryDirectory(prefix="plane-page-import-")
        import_root = Path(temp_dir.name)
        _safe_extract(source, import_root)
    else:
        import_root = source if source.is_dir() else source.parent

    try:
        markdown_files = _markdown_files(source if source.suffix.lower() in MARKDOWN_SUFFIXES else import_root)
        folder_ids: dict[Path, str | None] = {Path("."): parent_id}
        created_ids: list[str] = []
        results: list[ImportPageResult] = []
        errors: list[dict[str, str]] = []

        for markdown_path in markdown_files:
            relative = markdown_path.relative_to(import_root) if markdown_path != source else Path(markdown_path.name)
            current = Path(".")
            current_parent = parent_id
            for part in relative.parent.parts:
                current /= part
                if current not in folder_ids:
                    if dry_run:
                        folder_ids[current] = f"dry-run:{current.as_posix()}"
                    else:
                        folder_ids[current] = _create_folder(
                            client, workspace_slug, project_id, part, current_parent, access
                        )
                        created_ids.append(folder_ids[current] or "")
                current_parent = folder_ids[current]

            result = ImportPageResult(
                source=relative.as_posix(),
                name=_page_name(markdown_path),
                parent_id=current_parent,
            )
            if dry_run:
                results.append(result)
                continue

            try:
                page = client.pages._post(
                    _project_page_endpoint(workspace_slug, project_id),
                    {
                        "name": result.name,
                        "description_html": "<p></p>",
                        "parent": current_parent,
                        "access": access,
                    },
                )
                result.page_id = str(page["id"])
                created_ids.append(result.page_id)
                description_html, asset_count, warnings = _render_markdown_with_assets(
                    client=client,
                    workspace_slug=workspace_slug,
                    project_id=project_id,
                    page_id=result.page_id,
                    markdown_path=markdown_path,
                    import_root=import_root,
                    remote_images=remote_images,
                )
                client.pages._patch(
                    _project_page_endpoint(workspace_slug, project_id, result.page_id),
                    {"description_html": description_html},
                )
                result.assets_uploaded = asset_count
                result.warnings.extend(warnings)
                results.append(result)
            except (OSError, ValueError, HttpError) as exc:
                errors.append({"source": relative.as_posix(), "error": str(exc)})
                if on_error == "continue":
                    continue
                for created_id in reversed([value for value in created_ids if value]):
                    try:
                        _archive_page(client, workspace_slug, project_id, created_id)
                    except HttpError:
                        pass
                raise ValueError(f"Import failed for {relative.as_posix()}: {exc}") from exc

        return {
            "source": str(source),
            "dry_run": dry_run,
            "pages": [result.__dict__ for result in results],
            "errors": errors,
            "summary": {
                "pages_imported": 0 if dry_run else len([result for result in results if result.page_id]),
                "pages_planned": len(results),
                "assets_uploaded": sum(result.assets_uploaded for result in results),
                "warnings": sum(len(result.warnings) for result in results),
                "errors": len(errors),
            },
        }
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()
