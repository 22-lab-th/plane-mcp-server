from __future__ import annotations

import zipfile
from types import SimpleNamespace

import pytest

from plane_mcp import page_import

PNG_1X1 = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"


class FakePages:
    def __init__(self):
        self.posts = []
        self.patches = []
        self.counter = 0

    def _post(self, endpoint, payload):
        self.counter += 1
        self.posts.append((endpoint, payload))
        return {"id": f"id-{self.counter}"}

    def _patch(self, endpoint, payload):
        self.patches.append((endpoint, payload))
        return {"id": endpoint.rsplit("/", 1)[-1], **payload}


def test_safe_extract_rejects_path_traversal(tmp_path):
    bundle = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("../secret.md", "no")

    with pytest.raises(ValueError, match="Unsafe ZIP path"):
        page_import._safe_extract(bundle, tmp_path / "out")


def test_render_markdown_uploads_relative_image(monkeypatch, tmp_path):
    markdown = tmp_path / "guide.md"
    image = tmp_path / "images" / "diagram.png"
    image.parent.mkdir()
    image.write_bytes(PNG_1X1)
    markdown.write_text("# Guide\n\n![Diagram](images/diagram.png)\n", encoding="utf-8")
    monkeypatch.setattr(
        page_import,
        "_upload_asset",
        lambda *args, **kwargs: {"image_html": '<image-component src="asset-1"></image-component>'},
    )

    rendered, count, warnings = page_import._render_markdown_with_assets(
        client=SimpleNamespace(),
        workspace_slug="workspace",
        project_id="project",
        page_id="page",
        markdown_path=markdown,
        import_root=tmp_path,
        remote_images="copy",
    )

    assert '<image-component src="asset-1"></image-component>' in rendered
    assert count == 1
    assert warnings == []


def test_import_directory_preserves_folder_structure(monkeypatch, tmp_path):
    docs = tmp_path / "docs"
    nested = docs / "setup"
    nested.mkdir(parents=True)
    (nested / "install.md").write_text("# Install", encoding="utf-8")
    monkeypatch.setenv("PLANE_FILE_UPLOAD_ROOTS", str(tmp_path))
    pages = FakePages()

    report = page_import.import_markdown_path(
        client=SimpleNamespace(pages=pages),
        workspace_slug="workspace",
        project_id="project",
        file_path=str(docs),
    )

    assert report["summary"]["pages_imported"] == 1
    assert pages.posts[0][1] == {"name": "setup", "node_type": "folder", "parent": None, "access": 0}
    assert pages.posts[1][1]["parent"] == "id-1"
    assert pages.patches[0][1]["description_html"].startswith("<h1>Install</h1>")


def test_zip_import_dry_run_requires_no_api_calls(monkeypatch, tmp_path):
    bundle = tmp_path / "docs.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("guide.md", "# Guide")
        archive.writestr("images/ignored.png", PNG_1X1)
    monkeypatch.setenv("PLANE_FILE_UPLOAD_ROOTS", str(tmp_path))

    report = page_import.import_markdown_path(
        client=SimpleNamespace(),
        workspace_slug="workspace",
        project_id="project",
        file_path=str(bundle),
        dry_run=True,
    )

    assert report["summary"] == {
        "pages_imported": 0,
        "pages_planned": 1,
        "assets_uploaded": 0,
        "warnings": 0,
        "errors": 0,
    }
