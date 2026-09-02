"""CLI entry point for importing Markdown bundles into Plane Pages."""

from __future__ import annotations

import argparse
import json
import os

from plane import PlaneClient

from plane_mcp.compat import normalize_plane_base_url
from plane_mcp.page_import import import_markdown_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plane-page-import")
    parser.add_argument("source", help="Markdown file, directory, or ZIP bundle")
    parser.add_argument("--project", required=True, dest="project_id", help="Plane project UUID")
    parser.add_argument("--parent", dest="parent_id", help="Destination Page folder UUID")
    parser.add_argument("--access", type=int, default=0, help="Plane Page access level (default: 0/public)")
    parser.add_argument("--remote-images", choices=("copy", "keep"), default="copy")
    parser.add_argument("--on-error", choices=("stop", "continue"), default="stop")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=("json", "text"), default="text")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    api_key = os.getenv("PLANE_API_KEY", "")
    workspace_slug = os.getenv("PLANE_WORKSPACE_SLUG", "")
    if not api_key or not workspace_slug:
        raise SystemExit("PLANE_API_KEY and PLANE_WORKSPACE_SLUG are required")
    client = PlaneClient(
        base_url=normalize_plane_base_url(os.getenv("PLANE_BASE_URL", "https://api.plane.so")),
        api_key=api_key,
    )
    report = import_markdown_path(
        client=client,
        workspace_slug=workspace_slug,
        project_id=args.project_id,
        file_path=args.source,
        parent_id=args.parent_id,
        access=args.access,
        remote_images=args.remote_images,
        on_error=args.on_error,
        dry_run=args.dry_run,
    )
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    summary = report["summary"]
    print(
        f"Pages: {summary['pages_imported']} imported / {summary['pages_planned']} planned; "
        f"assets: {summary['assets_uploaded']}; warnings: {summary['warnings']}; errors: {summary['errors']}"
    )


if __name__ == "__main__":
    main()
