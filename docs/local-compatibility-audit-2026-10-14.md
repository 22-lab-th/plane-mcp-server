# Plane MCP compatibility audit, 14 October 2026

The local `plane_22lab` MCP works for core reads against `https://plane.22lab.dev`, workspace `projects`. Its 22lab extension operations are currently blocked by an older deployed backend. Updating or reconnecting the MCP alone will not expose those APIs.

This audit made no production writes, started no imports and changed no deployment or credentials. The only repository addition is this report. Findings distinguish live observations, local reproductions and source inspection.

## Versions and configuration

| Component | Observed version/state |
| --- | --- |
| Plane local checkout | `72c21ef4f0e0680e451024e6ad6d1caa3e4cbd52`, 2 October 2026 |
| MCP local checkout | `83311a6`, clean before this report |
| Installed MCP | `0.4.0`, imports this checkout |
| Dependencies in the configured virtual environment | Python 3.12, `plane-sdk 0.2.23`, `fastmcp 3.2.0`, `mcp 1.26.0` |
| Catalog | 36 resource tools, 282 actions |
| Codex transport | Local stdio, `.venv/bin/python -m plane_mcp stdio` |
| API destination | `https://plane.22lab.dev`, workspace `projects` |
| Allowed local upload root | `/Users/teeppiphat/Projects/22lab/life` only |
| Instance connector administration | Not enabled in this MCP connection's configured environment |

Codex also has a separate `plane` connection to the upstream cloud MCP. The tests in this report used `plane_22lab`.

Coolify reports the production Plane service and its API/worker components healthy. Its configured API, worker, beat-worker and migrator image is:

```text
plane22lab/backend@sha256:79af69091ae81165c5bf2faa923efa74ea6cd558721300695e1171ffbc47a470
```

That digest matches the saved deployment manifest at:

```text
/Users/teeppiphat/.local/share/plane-deployment/20260922T020510Z/release-manifest-deployed.json
```

The manifest identifies backend source commit `7cbf25af1bc20559e5306f9f6ec31cf4ff2f904d`. The configured image therefore predates the local `72c21ef4` token-extension commit. This was checked through Coolify service metadata and the saved manifest, without SSH or a container image inspection. Direct HTTP probes independently confirmed the absent APIs.

## Live read checks

Project-scoped checks used the accessible `CBUTR` project. Empty lists count as successful reads, not evidence that mutation workflows work.

| Operation | Result |
| --- | --- |
| `member me` | Success |
| `project list` | Success; total count 11, first page sampled |
| `workitem list`, with project | Success |
| `cycle list`, `module list`, `state list`, `label list` | Success |
| `view list`, `automation get` | Success |
| `page list`, `page retrieve`, with project | Success |
| `project_file list`, `get_storage` | HTTP 404 |
| `bookmark list` | HTTP 404 |
| `atlassian_import list_runs`, Confluence and Jira | HTTP 404 |
| `page get_summary`, `list_versions` | HTTP 404 |
| `workitem_type list`, `workspace get_features` | HTTP 404 |
| `workitem count`, workspace-wide `workitem list` | HTTP 404 |
| `workitem list` with PQL | Explicit unsupported-edition error |

The Files, Bookmarks and both import-history 404s were reproduced with direct authenticated GET requests to `/api/v1`, independently of the MCP SDK. Their response body was `{"error": "Page not found."}`.

The PQL restriction is intentional in the local Plane API as well: `apps/api/plane/api/views/issue.py` explicitly rejects `pql` and `filters` on this edition. Deploying the extension routes will not add all upstream cloud/edition features.

The existing `doctor` exited 0 with `connected=true`, core API HTTP 200 and Pages API HTTP 200. It currently checks only those two routes.

## Automated verification

Commands executed inside the MCP checkout:

```sh
rtk proxy sh -c 'env -u PLANE_TEST_API_KEY -u PLANE_TEST_WORKSPACE_SLUG .venv/bin/python -m pytest -q'
rtk proxy sh -c '.venv/bin/python -m ruff check .; .venv/bin/python -m ruff format --check .'
```

- Pytest: **1,607 passed, 35 skipped**, one Starlette/httpx deprecation warning.
- Ruff lint: passed.
- Ruff formatting: 98 files already formatted.
- Live write integration tests were deliberately disabled. No upload, content write, connector test, import, retry, sync, overwrite or deletion was run.
- The Plane Docker regression suite was not run in this audit. Passing MCP tests does not establish that the configured production backend exposes matching routes.

## Recommended work, in order

### 1. Upgrade the deployed Plane API and workers to a compatible release

**Priority: first operational prerequisite. Evidence: live reads plus deployment metadata.**

The MCP already implements Files/folders/versions/entity links, bookmarks/groups, extended Pages, Jira/Confluence runs and recovery, and optional instance connector administration. Reimplementing those tools is unnecessary.

The missing token routes are in the local Plane checkout:

- `apps/api/plane/api/urls/__init__.py` registers `extension_patterns`.
- `apps/api/plane/api/urls/extensions.py:69` publishes Files, Jira, Confluence/Page operations and bookmarks.
- The same file adds instance connector endpoints and project Page deletion.
- `docs/runbooks/plane-mcp-integration.md` describes coordinated deployment and smoke checks.

Prepare a tested release containing `72c21ef4`, review pending migrations, and coordinate API/worker upgrades with the required Live/storage services. Production deployment remains a separate authorized action. Recheck extension GETs after deployment, then verify writes in an isolated test project.

### 2. Add backend capability detection and expand `doctor`

**Priority: high. Evidence: live unsupported operations and current implementation.**

`plane_mcp/doctor.py:34` checks Projects and workspace Pages only. Extend it with a selected accessible project and read probes for Files, storage, bookmarks, Page summary/history and both import histories. Report core connectivity separately from extension readiness, with per-capability supported/unsupported/forbidden/unknown states. A single 404 on an entity must not establish that an entire capability is unavailable.

Use the same effective base URL logic as `client.py`, including `PLANE_INTERNAL_BASE_URL`. Adapt tool guidance/action availability to this backend: PQL, workspace-wide work-item listing, count, work-item types and workspace feature flags must not be presented as universally supported. The unconditional Epic recipe in `plane_mcp/instructions.py` depends on types and PQL, both unavailable here.

### 3. Make tool failure reporting consistent and actionable

**Priority: high. Evidence: live MCP results.**

`plane_mcp/extensions.py:30` converts HTTP errors into ordinary dictionaries. Live extension failures returned `isError=false` with `status_code=404`, while SDK-backed failures such as `workitem_type list` returned `isError=true`. A client that checks the MCP error flag can mistake an extension failure for success.

Use a consistent MCP error result while retaining structured API fields for permission failures, quota limits, active-run conflicts and recovery identifiers. Do not repeat writes after ambiguous failures.

The `upgrade_required` hint at `extensions.py:38` appears only when a 404 body is empty. Plane's actual nonempty `Page not found` body prevents it from appearing in the deployment mismatch found here. Determine upgrade guidance from capability checks instead. Conversely, `_page_api_error` in `tools/page.py:179` labels every Page 404 as a missing API, even when it may concern a specific resource.

### 4. Accept fractional Page ordering values

**Priority: medium. Evidence: reproduced locally without network writes.**

`plane_mcp/tools/page.py:365` declares `sort_order: int | None`, while Plane's `Page.sort_order` is a `FloatField` at `apps/api/plane/db/models/page.py:59`.

Running the registered tool with `action=move` and `sort_order=14.5` raised Pydantic `int_from_float` before its function executed. Change this to a floating-point field, validate finite values, and add a regression test for fractional ordering through the registered MCP tool.

### 5. Align workspace Page actions with the self-hosted API

**Priority: medium. Evidence: source/SDK contract comparison; no deletion attempted.**

`tools/page.py:418` calls SDK `delete_workspace_page` when `project_id` is omitted. The SDK sends `DELETE /workspaces/{slug}/pages/{id}/`, but the local Plane route allows only GET/PATCH (`apps/api/plane/api/urls/page.py:23`).

Likewise, workspace `archive=false` calls SDK `unarchive_workspace_page` (`tools/page.py:601`), which sends DELETE to a workspace `/archive/` route absent from that URL module. Only project archive/unarchive and project explicit deletion routes are present.

Gate these workspace operations with a clear unsupported-action result or add matching backend support with permission/tree tests. Deploying the current extension commit alone does not close this gap.

### 6. Add cross-repository contract and deployment smoke coverage

**Priority: high for release verification. Evidence: passing local suite alongside failing production extension reads.**

Keep existing unit tests, but add a job that runs the MCP client against the matching Plane test stack. Cover HTTP methods, public paths, field types, pagination, extension error flags, private-page/project permissions, file upload verification/activation and import progress/recovery.

Add a reusable read-only smoke command against the configured server, emitting a capability matrix and backend/MCP revision information. Reserve mutation scenarios for isolated fixtures/test projects. This would expose the release mismatch before a client sees a 36-tool catalog that its backend only partly supports.

### 7. Optional convenience and local setup changes

- Add stable Files/folder web links to MCP results or a helper action. The updated UI builds authenticated deep links in `apps/web/core/components/files/sharing.ts`; this is a UI link, not a public-access sharing API. The current MCP has expiring delivery URLs and durable `project-file:<id>` references, but no dedicated web-link action.
- Review the local upload roots when importing documents from other projects. The configured `life` root excludes both the Plane and MCP repositories. Add only intended import directories; do not broaden access to the entire home directory.
- Normalize Page hierarchy metadata for SDK consumers. A real retrieved Page included a non-null backend `parent` alongside SDK-default `parent_id=null`. Other 22lab fields were preserved through SDK `extra=allow`; they were not lost. A canonical parent field would avoid contradictory interpretations.

## Completion criteria for a follow-up implementation

1. Matching backend deployed through the normal release process; extension read probes succeed with the configured member token.
2. `doctor` reports partial readiness accurately and tool guidance reflects this Plane edition.
3. Tool errors have consistent MCP status and preserve actionable API details.
4. Fractional Page ordering passes, and workspace Page actions either have tested backend support or fail before issuing unsupported requests.
5. An isolated end-to-end run proves uploads/revisions/activation, Page write/history and a small Atlassian import/recovery workflow where configured.

The audit establishes current read connectivity and the gaps above. It does not certify production mutations, connector credentials, import workers or storage uploads.
