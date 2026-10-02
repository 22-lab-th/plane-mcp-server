from __future__ import annotations

import zipfile

import pytest

from plane_mcp import confluence_export as importer


def export(tmp_path, monkeypatch, *, cycle=False):
    bundle = tmp_path / "space.zip"
    parent = '<a href="Child_2.html">Child</a>' if cycle else ""
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("index.html", "<title>Delivery</title>")
        archive.writestr(
            "Home_1.html",
            f'<h1 id="title-text">Home</h1><div id="breadcrumbs">{parent}</div>'
            '<div id="main-content"><p>Before</p><img src="attachments/1/picture.png" width="320">'
            '<p>After</p><a href="Child_2.html">Child</a><script>alert(1)</script></div>',
        )
        archive.writestr(
            "Child_2.html",
            '<h1 id="title-text">Child</h1><div id="breadcrumbs">'
            '<a href="Home_1.html">Home</a></div><div id="main-content">'
            '<video><source src="attachments/2/demo.mp4"></video>'
            '<img src="attachments/1/picture.png"><a href="attachments/2/notes.pdf">Notes</a></div>',
        )
        archive.writestr("attachments/1/picture.png", b"image")
        archive.writestr("attachments/2/demo.mp4", b"video")
        archive.writestr("attachments/2/notes.pdf", b"pdf")
    monkeypatch.setenv("PLANE_FILE_UPLOAD_ROOTS", str(tmp_path))
    return str(bundle)


def test_export_dry_run_preserves_plan_without_network(tmp_path, monkeypatch):
    bundle = export(tmp_path, monkeypatch)
    monkeypatch.setattr(importer, "request", lambda *a, **kw: pytest.fail("dry run cannot call Plane"))
    result = importer.import_export(None, "acme", "p", bundle, dry_run=True)
    assert result["space_name"] == "Delivery"
    assert result["counts"] == {"total": 5, "completed": 0, "failed": 0}
    child = next(page for page in result["pages"] if page["title"] == "Child")
    assert child["parent"] == "Home_1.html"
    assert result["by_type"] == {"page": 2, "image/png": 1, "video/mp4": 1, "application/pdf": 1}


def test_export_media_order_hierarchy_links_and_shared_file_dedup(tmp_path, monkeypatch):
    bundle = export(tmp_path, monkeypatch)
    calls, uploads = [], []

    def api(client, method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs.get("data")))
        if method == "post" and endpoint.endswith("/pages"):
            return {"id": f"node-{len(calls)}"}
        return {}

    def upload(client, workspace, project, **kwargs):
        uploads.append(kwargs)
        return {"file": {"id": kwargs["name"]}}

    monkeypatch.setattr(importer, "request", api)
    monkeypatch.setattr(importer, "upload_bytes", upload)
    result = importer.import_export(None, "acme", "p", bundle)
    assert result["counts"] == {"total": 5, "completed": 5, "failed": 0, "remaining": 0}
    assert result["pages_imported"] == 2
    assert result["files_uploaded"] == 3
    assert len([item for item in uploads if item["name"] == "picture.png"]) == 1
    assert all(item["link"]["entity_type"] == "page" for item in uploads)
    home = next(
        data["description_html"]
        for method, endpoint, data in calls
        if method == "patch" and "Before" in data["description_html"]
    )
    assert home.index("Before") < home.index("project-file:picture.png") < home.index("After")
    assert 'width="320"' in home
    assert "<script" not in home
    assert "Child_2.html" not in home
    child = next(
        data["description_html"]
        for method, endpoint, data in calls
        if method == "patch" and "video-component" in data["description_html"]
    )
    assert "project-file:demo.mp4" in child
    assert "/files?file=notes.pdf" in child
    folder = next(
        data
        for method, endpoint, data in calls
        if data and data.get("name") == "Home" and data.get("node_type") == "folder"
    )
    assert folder["parent"] == "node-1"


def test_partial_file_failure_reports_reason_and_leaves_other_media(tmp_path, monkeypatch):
    bundle = export(tmp_path, monkeypatch)
    writes = []

    def api(client, method, endpoint, **kwargs):
        writes.append(kwargs.get("data", {}))
        return {"id": f"node-{len(writes)}"} if method == "post" and endpoint.endswith("/pages") else {}

    monkeypatch.setattr(importer, "request", api)
    monkeypatch.setattr(
        importer,
        "upload_bytes",
        lambda *a, **kw: {"error": "MIME type rejected"} if kw["name"] == "demo.mp4" else {"file": {"id": kw["name"]}},
    )
    result = importer.import_export(None, "acme", "p", bundle)
    assert result["counts"] == {"total": 5, "completed": 4, "failed": 1, "remaining": 0}
    failed = next(item for item in result["results"] if item["status"] == "failed")
    assert failed["error"] == "MIME type rejected"
    assert any("Media not imported" in item.get("description_html", "") for item in writes)


def test_hierarchy_cycle_rejected_before_creating_anything(tmp_path, monkeypatch):
    bundle = export(tmp_path, monkeypatch, cycle=True)
    monkeypatch.setattr(importer, "request", lambda *a, **kw: pytest.fail("invalid export must not write"))
    with pytest.raises(ValueError, match="Circular"):
        importer.import_export(None, "acme", "p", bundle)


def test_page_save_failure_reports_recovery_and_archives_only_new_nodes(tmp_path, monkeypatch):
    bundle = export(tmp_path, monkeypatch)
    archived, nodes = [], []

    def api(client, method, endpoint, **kwargs):
        if endpoint.endswith("/archive"):
            archived.append(endpoint)
            return {}
        if method == "patch":
            return {"error": "Live unavailable"}
        if endpoint.endswith("/pages"):
            nodes.append(f"node-{len(nodes)}")
            return {"id": nodes[-1]}
        return {}

    monkeypatch.setattr(importer, "request", api)
    monkeypatch.setattr(importer, "upload_bytes", lambda *a, **kw: {"file": {"id": kw["name"]}})
    result = importer.import_export(None, "acme", "p", bundle)
    assert result["phase"] == "Failed"
    assert result["created_page_ids"] == nodes
    assert len(archived) == len(nodes)
    assert result["counts"]["remaining"] > 0


def test_zip_duplicate_entries_are_rejected(tmp_path):
    from plane_mcp.page_import import _safe_extract

    bundle = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("Home.html", "first")
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("Home.html", "second")
    with pytest.raises(ValueError, match="duplicate file paths"):
        _safe_extract(bundle, tmp_path / "out")
