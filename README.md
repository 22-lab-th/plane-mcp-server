# Plane MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) server for
[Plane](https://plane.so). Gives an AI agent tools to read and manage projects,
work items, cycles, modules, releases, customers and more.

> This is the 22lab fork for the [22lab Plane distribution](https://github.com/22-lab-th/plane).
> Install this repository to use its extension tools; the upstream PyPI package and
> `mcp.plane.so` service do not include these additions.

Built on [FastMCP](https://github.com/jlowin/fastmcp) and the official
[`plane-sdk`](https://pypi.org/project/plane-sdk/).

- **36 tools**, one per Plane resource, covering 282 operations
- **Local or remote** — stdio, streamable HTTP, SSE
- **OAuth or API key** authentication

## Quick start

Get an API key from Plane: **Workspace Settings → API tokens**.

Add this to your MCP client's configuration:

```json
{
  "mcpServers": {
    "plane": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/22-lab-th/plane-mcp-server@main", "plane-mcp-server", "stdio"],
      "env": {
        "PLANE_API_KEY": "<your-api-key>",
        "PLANE_WORKSPACE_SLUG": "<your-workspace-slug>"
      }
    }
  }
}
```

`uvx` needs no install step. Requires Python 3.10+.

For a self-hosted Plane, add `"PLANE_BASE_URL": "https://plane.example.com"`.
Values ending in `/api` or `/api/v1` are also accepted and normalized to the
instance origin. Run `plane-mcp-server doctor` after setting the environment to
verify the core API and report whether the instance exposes API-token Pages.

## Transports

### stdio — local

Runs as a subprocess of your MCP client. Configuration as shown above; needs
`PLANE_API_KEY` and `PLANE_WORKSPACE_SLUG`.

```bash
PLANE_API_KEY=... PLANE_WORKSPACE_SLUG=... uv run plane-mcp-server stdio
```

### HTTP with OAuth — hosted

`https://mcp.plane.so/http/mcp`

The OAuth flow is handled on connect; no credentials in your config. For clients
without native remote MCP support, bridge with `mcp-remote`:

```json
{
  "mcpServers": {
    "plane": {
      "command": "npx",
      "args": ["mcp-remote@latest", "https://mcp.plane.so/http/mcp"]
    }
  }
}
```

Requires Node.js 22+.

### HTTP with a personal access token — hosted

`https://mcp.plane.so/http/api-key/mcp`

| Header | Value |
|---|---|
| `Authorization` | `Bearer <PAT>` |
| `X-Workspace-slug` | `<workspace-slug>` |

```json
{
  "mcpServers": {
    "plane": {
      "command": "npx",
      "args": ["mcp-remote@latest", "https://mcp.plane.so/http/api-key/mcp"],
      "headers": {
        "Authorization": "Bearer <PAT>",
        "X-Workspace-slug": "<workspace-slug>"
      }
    }
  }
}
```

### SSE — deprecated

`https://mcp.plane.so/sse` is maintained for backward compatibility only. Use an
HTTP transport instead.

## Tools

The server advertises 36 tools, one per resource. Each takes an `action`
parameter that selects the operation:

```python
workitem(action="create", project_id=..., name="Fix login")
workitem(action="list", project_id=..., pql='state__group = "started"')
cycle(action="archive", project_id=..., cycle_id=...)
```

Every tool's description lists its actions with their required and optional
parameters, so the catalogue is self-documenting at call time.

**→ [Full tool and action reference](plane_mcp/tools/README.md)**

### 22lab feature coverage

The `project_file` tool manages Project Files, folders, versions, entity links,
storage usage, audit history and verified uploads. `bookmark` manages shared
workspace bookmarks and groups. The expanded `page` tool supports nested folders,
document moves, locks/access, favorites, duplicates and version history.

`atlassian_import` imports Confluence spaces and Jira projects through Atlassian
APIs, including attachments and Jira sprints as Cycles. It exposes background
progress, type counts, failure reasons, retry by item, sync and overwrite.
Confluence HTML ZIP imports preserve hierarchy and media positions. `instance`
provides explicitly enabled, administrator-only God Mode connector configuration.

**→ [Setup, feature mapping and examples](docs/22lab-features.md)**

These extension actions require the matching 22lab Plane backend. Core upstream
tools remain available subject to backend edition and permissions. Interactive
God Mode login and OIDC setup remain in the Plane UI.

### Querying work items

List, count and search accept **PQL**, Plane's query language:

```python
workitem(action="list", project_id=..., pql='state__group = "started" AND priority = "urgent"')
workitem(action="count", pql='assignees__id = "<member id>"', group_by="state_id")
```

Call `get_pql_reference` for the full syntax, operators and worked examples.

### Importing Markdown with images

The `page` tool can import one Markdown file, a directory, or a ZIP bundle while
preserving document folders and storing referenced PNG, JPEG, GIF, and WebP
images as Page assets:

```python
page(
    action="import_markdown_bundle_from_path",
    project_id="<project uuid>",
    file_path="/allowed/docs.zip",
    remote_images="copy",
    on_error="stop",
)
```

Local sources must be inside `PLANE_FILE_UPLOAD_ROOTS`. Use `dry_run=true` to
inspect the plan without creating anything. ZIP imports reject traversal paths,
symlinks, oversized archives, and excessive entry counts. When `on_error` is
`stop`, newly created Pages and folders are archived on failure.

The same importer is available as a CLI:

```bash
plane-page-import ./docs.zip --project <project-uuid> --dry-run
plane-page-import ./docs --project <project-uuid> --remote-images copy
```

### Upgrading from the per-operation tools

Earlier releases exposed one tool per API operation. **Existing integrations keep
working**: 169 of those 177 names still resolve to the consolidated tool, so a
saved prompt or script calling `create_work_item` or `list_cycles` needs no
change. They are no longer advertised, and they keep the parameter names they
shipped with (`work_item_id`, not `workitem_id`).

Seven names chose between two operations with a parameter
(`manage_project_archive(archive=False)`), which one tool-and-action pair cannot
reproduce; calling one tells you its replacement. `get_pql_reference` is
unchanged.

## Configuration

### Authentication

| Variable | Required for | Purpose |
|---|---|---|
| `PLANE_API_KEY` | stdio | API key |
| `PLANE_WORKSPACE_SLUG` | stdio | Target workspace |
| `PLANE_BASE_URL` | optional | Plane API URL (default `https://api.plane.so`) |
| `PLANE_FILE_UPLOAD_ROOTS` | local uploads/imports | Allowed roots separated by `:` on macOS/Linux or `;` on Windows; paths belong to the MCP server host |
| `PLANE_PROJECT_FILE_UPLOAD_MAX_BYTES` | optional | Local Project File limit, default 104857600 bytes; backend limits also apply |
| `PLANE_STORAGE_UPLOAD_HOSTS` | private storage | Comma-separated trusted upload hostnames, e.g. `localhost,minio`; no ports |
| `PLANE_ENABLE_INSTANCE_TOOLS` | optional admin tools | Set `1` to permit instance connector configuration; disabled by default |
| `PLANE_CONFLUENCE_API_TOKEN` / `PLANE_JIRA_API_TOKEN` | stdio connector setup | Optional Atlassian secrets read only by explicit `token_from_env=true` |

The remote transports carry credentials in the connection — the OAuth flow or the
PAT headers — and need none of these.

Self-hosting the server itself:

| Variable | Purpose |
|---|---|
| `PLANE_INTERNAL_BASE_URL` | Internal URL for server-to-server calls, preferred over `PLANE_BASE_URL` |
| `REDIS_HOST` / `REDIS_PORT` | OAuth token storage; falls back to in-memory |
| `PLANE_OAUTH_PROVIDER_*` | OAuth client credentials and base URL |
| `PLANE_OAUTH_ENABLED` | `auto` (default), `true`, or `false`; use `false` for PAT-only self-hosted deployments |
| `PLANE_ALLOWED_WORKSPACE_SLUGS` | Optional comma-separated allowlist enforced by the PAT transport |
| `MCP_PATH_PREFIX` | Path prefix for the HTTP routes, when mounted behind a proxy — `/plane` serves `/plane/http/mcp` |

Self-hosted Plane releases without OAuth application endpoints can run a shared,
header-authenticated server without holding a central Plane credential:

```bash
docker build -t plane-mcp-server .
docker run --rm -p 8211:8211 \
  -e PLANE_BASE_URL=https://plane.example.com \
  -e PLANE_OAUTH_ENABLED=false \
  -e PLANE_ALLOWED_WORKSPACE_SLUGS=my-workspace \
  plane-mcp-server
```

Each member connects to `https://your-mcp-host/mcp` with their own headers:
`Authorization: Bearer <personal Plane API token>` and
`X-Workspace-Slug: my-workspace`. Tokens remain revocable per member and are
never stored by the MCP server.

### OAuth redirect URIs

The OAuth transports validate each client's redirect URI against an allowlist.
Common clients (Cursor, VS Code, Claude.ai, ChatGPT connectors, localhost) are
allowed by default.

To onboard a new client without a release, append patterns:

```bash
export PLANE_OAUTH_ALLOWED_REDIRECT_URIS="https://newclient.com/cb,https://other.app/oauth/*"
```

`*` matches any port, path segment or subdomain. Keep the host pinned and
wildcard only the port or path.

### Logging

Structured JSON. Each tool call logs its name, duration, status and — when
available — an opaque user id and the workspace slug.

```bash
export LOG_USER_INFO=false   # do not log display names
export LOG_PAYLOADS=false    # omit tool request/result payloads; default true
```

Only the OAuth and PAT transports carry a display name; stdio is unaffected.

## Development

```bash
git clone https://github.com/22-lab-th/plane-mcp-server
cd plane-mcp-server
uv pip install -e ".[dev]"
```

Run the server against a workspace:

```bash
PLANE_API_KEY=... PLANE_WORKSPACE_SLUG=... python -m plane_mcp stdio
python -m plane_mcp http            # port 8211
```

Tests, format, lint:

```bash
pytest                              # no network or credentials needed
ruff format plane_mcp/ tests/       # line length 120
ruff check plane_mcp/ tests/        # rules E, F, I, UP, B
```

The suite runs fully offline — every action of every resource is
executed against a stand-in that binds each call against the genuine `plane-sdk`
signature. See [`plane_mcp/tools/README.md`](plane_mcp/tools/README.md#tests).

Live integration tests are skipped unless you point them at a running server:

```bash
export PLANE_TEST_API_KEY=... PLANE_TEST_WORKSPACE_SLUG=...
export PLANE_TEST_MCP_URL=http://localhost:8211    # optional; this is the default
pytest tests/test_integration.py -v
```

They write real data to that workspace.

### Repository layout

| Path | Contents |
|---|---|
| `plane_mcp/__main__.py` | entry point; picks the transport from `argv[1]` |
| `plane_mcp/server.py` | one factory per transport |
| `plane_mcp/client.py` | resolves credentials into a `plane-sdk` client |
| `plane_mcp/auth/` | OAuth provider and header auth |
| `plane_mcp/tools/` | the tool surface: one module per Plane resource |
| `plane_mcp/toolkit/` | shared building blocks for the tool surface |
| `plane_mcp/pql_reference.py` | PQL syntax reference served to models |

## Contributing

Pull requests welcome. Please run `pytest` and `ruff check` before submitting; new
tools should come with the invariants described in
[`plane_mcp/tools/README.md`](plane_mcp/tools/README.md).

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Migrating from the Node.js server

`@makeplane/plane-mcp-server` (Node.js) is deprecated and unmaintained. This
Python implementation replaces it.

| Node.js | Python |
|---|---|
| `PLANE_API_KEY` | `PLANE_API_KEY` |
| `PLANE_API_HOST_URL` | `PLANE_BASE_URL` |
| `PLANE_WORKSPACE_SLUG` | `PLANE_WORKSPACE_SLUG` |

Replace the `command` and `args` with the stdio configuration in
[Quick start](#quick-start).

## License

MIT — see [LICENSE](LICENSE).
