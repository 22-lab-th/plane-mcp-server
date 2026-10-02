"""Import an offline Confluence HTML ZIP, preserving hierarchy and media order."""

from __future__ import annotations

import html
import mimetypes
import re
import tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from plane_mcp.extensions import project_path, request, segment
from plane_mcp.page_import import _allowed_import_path, _safe_extract
from plane_mcp.project_files import upload_bytes

ALLOWED = set(
    (
        "p br h1 h2 h3 h4 h5 h6 strong b em i u s del code pre blockquote "
        "ul ol li table thead tbody tr th td hr a span div"
    ).split()
)
REMOVED = set("script style iframe object embed form input button link meta base noscript".split())
IMAGES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/tiff"}
VIDEOS = {"video/mp4", "video/webm", "video/ogg"}


def local_path(root: Path, page: Path, source: str) -> Path | None:
    if not source or source.startswith("#"):
        return None
    parsed = urlsplit(source)
    if parsed.scheme or parsed.netloc or source.startswith("/"):
        attachment = re.search(r"/download/attachments/([^?#]+)", source)
        if not attachment:
            return None
        relative = "attachments/" + attachment.group(1)
    else:
        relative = parsed.path
    candidate = (page.parent / unquote(relative)).resolve()
    return candidate if candidate.is_relative_to(root.resolve()) else None


def read_space(root: Path, default_name: str):
    root = root.resolve()
    files = sorted(path for path in root.rglob("*") if path.is_file())
    if any(path.name.lower() == "entities.xml" for path in files):
        raise ValueError("XML exports are not supported; export the space as HTML with attachments")
    pages = {}
    name = default_name
    for path in files:
        if path.suffix.lower() not in {".html", ".htm"} or "attachments" in path.relative_to(root).parts:
            continue
        if path.stat().st_size > 5 * 1024 * 1024:
            raise ValueError("Confluence HTML pages must be at most 5 MB")
        soup = BeautifulSoup(path.read_text(encoding="utf-8-sig", errors="replace"), "html.parser")
        title = soup.select_one("#title-text") or soup.title
        title = title.get_text(strip=True) if title else path.stem
        content = soup.select_one("#main-content, .wiki-content")
        if content is None:
            if path.stem.lower() == "index":
                name = title or name
            continue
        crumbs = soup.select("#breadcrumbs a[href], .breadcrumbs a[href]")
        parent = local_path(root, path, crumbs[-1]["href"]) if crumbs else None
        pages[path] = {"title": title[:255], "content": content, "parent": parent}
    if not pages:
        raise ValueError("No Confluence pages found; use an HTML space export with attachments")
    for path, page in pages.items():
        if page["parent"] not in pages or page["parent"] == path:
            page["parent"] = None
        seen = {path}
        parent = page["parent"]
        while parent in pages:
            if parent in seen or len(seen) > 64:
                raise ValueError("Circular or excessively deep page hierarchy in the export")
            seen.add(parent)
            parent = pages[parent]["parent"]
    return name[:255], pages, files


def media_sources(node: Tag) -> list[str]:
    if node.name == "video":
        return [node.get("src", ""), *(child.get("src", "") for child in node.select("source"))]
    if node.name == "object":
        param = node.select_one('param[name="movie"], param[name="src"]')
        return [node.get("data", ""), param.get("value", "") if param else ""]
    return [node.get("href" if node.name == "a" else "src", "")]


def safe_html(content: Tag, replacements: dict, root: Path, path: Path, links: dict, warnings: list) -> str:
    def render(node):
        if isinstance(node, NavigableString):
            return html.escape(str(node))
        if not isinstance(node, Tag):
            return ""
        if id(node) in replacements:
            return replacements[id(node)]
        if node.get("id") == "footer" or set(node.get("class", [])) & {"page-metadata", "attachments", "toc-macro"}:
            return ""
        if node.name in {"img", "video", "object", "embed"}:
            source = next((value for value in media_sources(node) if value), "missing source")
            warnings.append(f"{path.name}: media missing from export: {source}")
            return f"<p>Media not imported: {html.escape(source)}</p>"
        if node.name in REMOVED or node.name == "source":
            return ""
        tag = node.name if node.name in ALLOWED else "span"
        attrs = {}
        if tag == "a":
            source = node.get("href", "")
            destination = local_path(root, path, source)
            if destination in links:
                attrs["href"] = links[destination] + (
                    "#" + urlsplit(source).fragment if urlsplit(source).fragment else ""
                )
            elif re.match(r"^(https?://|mailto:|tel:|#)", source, re.I):
                attrs["href"] = source
            elif source:
                warnings.append(f"{path.name}: link target not imported: {source}")
        attrs.update({key: node[key] for key in ("id", "colspan", "rowspan", "start", "language") if key in node.attrs})
        attributes = "".join(f' {key}="{html.escape(str(value), quote=True)}"' for key, value in attrs.items())
        body = "".join(render(child) for child in node.children)
        return f"<{tag}{attributes}>" + ("" if tag in {"br", "hr"} else body + f"</{tag}>")

    return "".join(render(child) for child in content.children) or "<p></p>"


def import_export(client, workspace: str, project: str, file_path: str, *, parent_id=None, access=0, dry_run=False):
    bundle = _allowed_import_path(file_path)
    if not bundle.is_file() or bundle.suffix.lower() != ".zip":
        raise ValueError("Choose a Confluence HTML space export ZIP")
    with tempfile.TemporaryDirectory(prefix="plane-confluence-") as temporary:
        root = Path(temporary).resolve()
        _safe_extract(bundle, root)
        name, pages, files = read_space(root, bundle.stem)
        attachments = {path for path in files if "attachments" in path.relative_to(root).parts}
        for path, page in pages.items():
            for node in page["content"].select("img, video, object, embed, a[href]"):
                for source in media_sources(node):
                    candidate = local_path(root, path, source)
                    if candidate in files and candidate not in pages:
                        attachments.add(candidate)
        types = Counter({"page": len(pages)})
        for path in attachments:
            types[mimetypes.guess_type(path.name)[0] or "application/octet-stream"] += 1
        report = {
            "space_name": name,
            "dry_run": dry_run,
            "phase": "Ready" if dry_run else "Importing",
            "counts": {"total": len(pages) + len(attachments), "completed": 0, "failed": 0},
            "by_type": dict(types),
            "pages_imported": 0,
            "files_uploaded": 0,
            "page_ids": [],
            "results": [],
            "warnings": [],
        }
        if dry_run:
            report["pages"] = [
                {
                    "source": str(path.relative_to(root)),
                    "title": page["title"],
                    "parent": str(page["parent"].relative_to(root)) if page["parent"] else None,
                }
                for path, page in pages.items()
            ]
            return report
        endpoint = project_path(workspace, project, "pages")
        created, folders, ids, uploaded, failed = [], {}, {}, {}, set()
        base = f"/{segment(workspace)}/projects/{segment(project)}"

        def create(data):
            result = request(client, "post", endpoint, data=data)
            if not isinstance(result, dict) or not result.get("id"):
                raise ValueError("Plane could not create a page/folder; review permissions and the destination")
            created.append(result["id"])
            return result["id"]

        try:
            space_folder = create({"name": name, "node_type": "folder", "access": access, "parent": parent_id})

            def ensure_folder(path):
                if path is None:
                    return space_folder
                if path not in folders:
                    page = pages[path]
                    folders[path] = create(
                        {
                            "name": page["title"],
                            "node_type": "folder",
                            "access": access,
                            "parent": ensure_folder(page["parent"]),
                        }
                    )
                return folders[path]

            ancestors = {page["parent"] for page in pages.values() if page["parent"]}
            for path, page in pages.items():
                parent = ensure_folder(path if path in ancestors else page["parent"])
                ids[path] = create(
                    {"name": page["title"], "parent": parent, "access": access, "description_html": "<p></p>"}
                )
            links = {path: f"{base}/pages/{identifier}" for path, identifier in ids.items()}

            def upload(path, owner=None):
                if path in failed:
                    return None
                file_root = project_path(workspace, project, "files")
                if path in uploaded:
                    if owner:
                        linked = request(
                            client,
                            "post",
                            f"{file_root}/{segment(uploaded[path])}/links",
                            data={"entity_type": "page", "entity_id": owner},
                        )
                        if isinstance(linked, dict) and linked.get("error"):
                            report["warnings"].append(f"{path.name}: shared file could not be linked to this page")
                    return uploaded[path]
                mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                result = upload_bytes(
                    client,
                    workspace,
                    project,
                    name=path.name,
                    payload=path.read_bytes(),
                    mime_type=mime,
                    link={"entity_type": "page", "entity_id": owner} if owner else None,
                )
                identifier = result.get("file", {}).get("id") if isinstance(result, dict) else None
                row = {
                    "source": str(path.relative_to(root)),
                    "kind": mime,
                    "status": "completed" if identifier else "failed",
                }
                if identifier:
                    uploaded[path] = identifier
                    report["files_uploaded"] += 1
                    report["counts"]["completed"] += 1
                    links[path] = f"{base}/files?file={identifier}"
                    row["file_id"] = identifier
                else:
                    failed.add(path)
                    report["counts"]["failed"] += 1
                    row["error"] = (
                        result.get("error", "File upload failed") if isinstance(result, dict) else "File upload failed"
                    )
                    report["warnings"].append(f"{path.name}: {row['error']}")
                report["results"].append(row)
                return identifier

            for path, page in pages.items():
                replacements = {}
                for node in page["content"].select("img, video, object, embed, a[href]"):
                    if node.name == "a" and node.select_one("img, video, object, embed"):
                        continue
                    attachment = next(
                        (
                            candidate
                            for source in media_sources(node)
                            if (candidate := local_path(root, path, source)) in attachments
                        ),
                        None,
                    )
                    if attachment is None:
                        continue
                    identifier = upload(attachment, ids[path])
                    if not identifier:
                        continue
                    mime = mimetypes.guess_type(attachment.name)[0] or "application/octet-stream"
                    tag = (
                        "image-component"
                        if node.name == "img" and mime in IMAGES
                        else "video-component"
                        if mime in VIDEOS
                        else "a"
                    )
                    if tag == "a":
                        label = html.escape(node.get_text(strip=True) or attachment.name)
                        replacements[id(node)] = f'<a href="{html.escape(links[attachment], quote=True)}">{label}</a>'
                    else:
                        dimensions = "".join(
                            f' {key}="{int(node[key])}"'
                            for key in ("width", "height")
                            if str(node.get(key, "")).isdigit() and 0 < int(node[key]) <= 10000
                        )
                        replacements[id(node)] = (
                            f'<{tag} src="project-file:{html.escape(identifier, quote=True)}"'
                            f' status="uploaded" alignment="center"{dimensions}></{tag}>'
                        )
                content = safe_html(page["content"], replacements, root, path, links, report["warnings"])
                saved = request(client, "patch", f"{endpoint}/{segment(ids[path])}", data={"description_html": content})
                if isinstance(saved, dict) and saved.get("error"):
                    raise ValueError("Plane could not save the page content; check the Live conversion service")
                report["pages_imported"] += 1
                report["page_ids"].append(ids[path])
                report["counts"]["completed"] += 1
                report["results"].append(
                    {"source": str(path.relative_to(root)), "kind": "page", "status": "completed", "page_id": ids[path]}
                )
            for path in sorted(attachments):
                if path not in uploaded and path not in failed:
                    upload(path)
            report["phase"] = "Finished with warnings" if report["warnings"] else "Completed"
        except Exception as exc:
            # Retain uploaded Files for recovery; archive just this import's nodes.
            report["phase"] = "Failed"
            report["error"] = "Import stopped; newly created pages were archived where possible. Uploaded files remain."
            if isinstance(exc, ValueError):
                report["failure_reason"] = str(exc)
            report["created_page_ids"] = created
            report["rollback_failed_ids"] = []
            for identifier in reversed(created):
                try:
                    result = request(client, "post", f"{endpoint}/{segment(identifier)}/archive", data={})
                    if isinstance(result, dict) and result.get("error"):
                        report["rollback_failed_ids"].append(identifier)
                except Exception:
                    report["rollback_failed_ids"].append(identifier)
        report["counts"]["remaining"] = (
            report["counts"]["total"] - report["counts"]["completed"] - report["counts"]["failed"]
        )
        return report
