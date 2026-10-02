# 22lab Plane features through MCP

This fork exposes 36 tools and 282 actions. The full action inventory is in
[the tool reference](../plane_mcp/tools/README.md). New extension actions require
the matching 22lab Plane backend with token routes under `/api/v1`. Install both
repositories' current `main` versions; upgrading the MCP process alone does not
add backend routes. Existing upstream tools retain their edition/plan requirements.

## Install and connect

```bash
git clone https://github.com/22-lab-th/plane-mcp-server.git
cd plane-mcp-server
uv sync --extra dev
```

For a local checkout, an MCP client can use:

```json
{
  "mcpServers": {
    "plane": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/plane-mcp-server", "run", "plane-mcp-server", "stdio"],
      "env": {
        "PLANE_BASE_URL": "http://127.0.0.1:8000",
        "PLANE_API_KEY": "<Plane API token>",
        "PLANE_WORKSPACE_SLUG": "22lab",
        "PLANE_FILE_UPLOAD_ROOTS": "/absolute/path/imports",
        "PLANE_STORAGE_UPLOAD_HOSTS": "localhost,127.0.0.1"
      }
    }
  }
}
```

Use the API origin or the reverse proxy origin that serves `/api/v1`. Local files
are read on the MCP host; allow only the directories intended for imports. For
HTTP deployment, mount those directories explicitly. Restart/reconnect clients
after upgrading so their tool listing refreshes. `plane-mcp-server doctor` checks
core connectivity and Pages routes; it does not certify all extension routes.

Shared HTTP servers support individual PATs and `PLANE_OAUTH_ENABLED=false`.
Members supply `Authorization: Bearer <Plane API token>` and
`X-Workspace-Slug: 22lab`. Each operation runs as that token's owner. A token
bound to another workspace or inactive membership is rejected. Browser login
cookies and SSO sessions are not accepted as MCP credentials.

## Feature coverage

| Plane feature | MCP interface |
|---|---|
| Project/work-item CRUD, statuses, labels, assignees, links, comments, attachments | Existing `project`, `workitem`, `state`, `label`, `workitem_relation`, `workitem_comment`, `workitem_attachment` tools |
| Cycles, Modules, estimates, views and automation | `cycle`, `module`, `project_estimate`, `view`, `automation` |
| Pages and nested folders | `page`: create/update/list/retrieve, `create_folder`, `move`, archive/restore/delete |
| Page permissions and organization | `page`: lock/unlock, set_access, favorite/unfavorite, duplicate, move_to_project, get_summary |
| Page history | `page`: list_versions, retrieve_version; version restore can be performed by updating content after review |
| Markdown file/folder/ZIP and images | `page`: import_markdown_from_path, import_markdown_bundle_from_path, image asset upload/read/delete |
| Project Files | `project_file`: search/filter, folder tree, preview/download, storage usage, activity, copy/move, trash/restore/purge |
| File versions and links | `project_file`: upload revision, list_versions, activate_version, link/unlink, list_entity_links |
| Workspace Bookmarks | `bookmark`: CRUD/search, group CRUD, URL metadata |
| Confluence API and HTML ZIP imports | `atlassian_import`: list_spaces, start, import_html_export_from_path |
| Jira API import, including sprints as Cycles | `atlassian_import`: list_projects, list_members, start with user_mapping |
| Import progress and recovery | `atlassian_import`: list_runs, retrieve_run, retry_failed, retry_selected, sync, overwrite |
| God Mode Atlassian connector settings | `instance`: get_connector, update_connector, test_connector; instance admin only |
| Edition-specific features | Existing initiatives, releases, customers, collections, templates and other tools remain available where the backend supports them |

Interactive God Mode login, instance installation, OIDC login/setup, theme and
editor UI controls remain in Plane's UI. They are not advertised as MCP actions.
The MCP token interface does not bypass instance, project, file or private-page
permissions. Project Files preserve quotas, MIME validation, archived-project
read-only rules, version integrity and audit records.

## Confluence and Jira API workflow

Configure **God Mode → Confluence/Jira** first. Then select a destination Plane
project and discover the remote source:

```python
atlassian_import(action="list_spaces", project_id="<Plane project UUID>", search="Team Docs")
atlassian_import(action="list_projects", project_id="<Plane project UUID>", offset=0)
atlassian_import(action="list_members", project_id="<Plane project UUID>")
```

Confluence search includes authorized spaces beyond the initial page; follow
`next_cursor` if returned. Jira returns `next_offset`. Use the numeric source ID,
not a space/project name or Jira key, when starting:

```python
atlassian_import(action="start", provider="confluence", project_id="<Plane project UUID>",
                 remote_id="123456", access=0)
atlassian_import(action="start", provider="jira", project_id="<Plane project UUID>",
                 remote_id="10001", user_mapping=[
                     {"account_id": "<Jira account ID>", "member_id": "<Plane member UUID>"}
                 ])
```

The response is a queued run with `id` and `source_id`. Import continues in the
backend worker after the MCP call returns or the client disconnects. Poll:

```python
atlassian_import(action="retrieve_run", provider="jira", project_id="<Plane project UUID>",
                 run_id="<run UUID>", offset=0)
```

The response contains `run`, `results`, `count` and `next_offset`. `run` includes
phase, status, inventory_complete, total/completed/failed/skipped/pending/running
counts, type breakdowns, and run failure information. Remaining work is pending
plus running; totals are provisional until `inventory_complete=true`. Each
result includes `error_code` and `error_message`. Follow `next_offset` to read all
results; `status="failed"` filters failures. `list_runs` shows the latest 20 runs.
After an uncertain timeout, read history before submitting another start.

```python
atlassian_import(action="retry_failed", provider="confluence", project_id="<project>", source_id="<source>")
atlassian_import(action="retry_selected", provider="confluence", project_id="<project>", source_id="<source>",
                 item_ids=["<result id>"])
atlassian_import(action="sync", provider="jira", project_id="<project>", source_id="<source>")
atlassian_import(action="overwrite", provider="jira", project_id="<project>", source_id="<source>", confirm=True)
```

Selected IDs are `results[].id`, not Atlassian remote IDs. Sync imports new,
changed and previously failed items. Overwrite replaces imported content,
including local edits, while retaining destination IDs and previous versions.
Only one run can be active per source. Retry of attachments also repairs their
dependent pages or work items. Deleted remote items are not automatically removed.

Confluence preserves hierarchy and media positions. Jira imports work items,
statuses, priorities, labels, mapped assignees, parent/subtask relationships,
issue links, comments, attachments and sprints as Cycles. Destination permissions
apply; Jira visibility restrictions, custom fields/workflows/worklogs and full
historical sprint membership are not migrated by the backend importer.

## Offline Confluence HTML export

```python
atlassian_import(action="import_html_export_from_path", project_id="<project>",
                 file_path="/allowed/space.zip", dry_run=True)
```

Remove `dry_run` after reviewing the plan. Export HTML with attachments, not XML.
The importer creates a space folder and nested documents, rewrites internal page
links, stores attachments in Project Files, and inserts image/video components
at their original positions using durable `project-file:<id>` references. Shared
attachments are uploaded once and linked to their pages. It does not fetch remote
media that are absent from the ZIP. Missing media and failed files are reported.

Limits: 50 MiB ZIP, 100 MiB expanded, 500 entries, 5 MiB per HTML page, hierarchy
depth 64. Traversal, symlinks, duplicate paths and cyclic hierarchy are rejected.
The synchronous result includes totals, type counts, per-file failures and warnings.
Fatal failure archives only newly created nodes where possible, leaves uploaded
Files for recovery, and returns recovery IDs. Every ZIP import creates a new copy;
use API imports for incremental sync and selected retries.

## Files, versions and media

```python
project_file(action="list", project_id="<project>", filters={"q": "diagram", "pinned": False}, per_page=50)
project_file(action="upload_from_path", project_id="<project>", file_path="/allowed/diagram.png",
             entity_type="page", entity_id="<page>")
project_file(action="upload_from_path", project_id="<project>", file_id="<file>", file_path="/allowed/revision.png")
project_file(action="activate_version", project_id="<project>", file_id="<file>", version_no=2)
```

The upload helper reserves quota, streams a signed PUT with exact headers, then
asks Plane to verify bytes, checksum and type. A PUT failure triggers abort and
returns reserved IDs for recovery; Plane tokens are never forwarded to storage.
New file revisions need explicit activation. Manual clients may use
initiate_upload → PUT → complete_upload, or abort_upload. Keep signed delivery
URLs transient. Persist image/video components with `src="project-file:<file UUID>"`.

`list` returns file/folder/breadcrumb information and a nested `page.next_cursor`;
activity uses the same cursor pagination. `to_root=true` clears a folder/parent;
`is_pinned=false`, zero sizes and `remark=""` are preserved rather than omitted.
Purge and cross-project move require `confirm=true`; recursive folder deletion
also requires confirmation. Review the target and local edits before confirming.

## Instance connector administration

Use an **unscoped Plane API token owned by an instance administrator** and enable
`PLANE_ENABLE_INSTANCE_TOOLS=1`. A workspace-bound token cannot administer the
instance, even if its owner is an instance admin.

```python
instance(action="get_connector", provider="confluence")
instance(action="update_connector", provider="confluence", enabled=True,
         site_url="https://example.atlassian.net", email="<account email>", confirm=True)
instance(action="test_connector", provider="confluence")
```

Saved secrets are redacted. Omit token options to retain the saved secret. Local
stdio administrators may set `PLANE_CONFLUENCE_API_TOKEN`/`PLANE_JIRA_API_TOKEN`
on the server and request `token_from_env=true`; remote callers cannot read those
environment secrets. Do not send secrets as tool arguments. Disable a connector
before `clear_token=true`. Connection tests validate saved access, not visibility
of every remote item. Atlassian settings request bodies are redacted in backend
API logs.

## Upgrade and verification

Pull both repositories, apply required Plane migrations, restart the Plane API
and worker, run `uv sync` in this MCP checkout, and restart the MCP process/client.
An absent extension route returns an API error; install the matching backend
instead of substituting a browser session. A 403 may indicate membership or role;
409 may describe a running import, archived project or unavailable file version.
Preserve those failure reasons when presenting results to the user.

```bash
uv run pytest -q
uv run ruff check plane_mcp tests
uv run ruff format --check plane_mcp tests
```

The offline suite validates the complete action catalog, real SDK argument types,
MCP wire serialization, import modes, signed upload lifecycle, secret isolation,
ZIP bounds, hierarchy and media positions. Plane's backend suite independently
checks public token routing, workspace/role boundaries and storage/import contracts.
