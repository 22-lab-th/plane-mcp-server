# Local MCP setup for plane.22lab.dev

This checkout is prepared to run as a local stdio MCP server against the
self-hosted Plane instance at `https://plane.22lab.dev`.

## Configure Codex

Use `config/codex-plane-22lab.toml.example` as the MCP settings template. Set:

- `PLANE_WORKSPACE_SLUG` to the workspace slug shown in the Plane URL.
- `PLANE_API_KEY` to a token created in Workspace Settings → API tokens.
- `PLANE_BASE_URL` to `https://plane.22lab.dev`.
- `PLANE_FILE_UPLOAD_ROOTS` to an OS-path-separated list of directories from
  which the local MCP may upload Page images. Local uploads stay disabled when
  this setting is omitted.

Do not put the token in this repository or a shell command. Enter it only in
the MCP settings/environment field.

## Verify before enabling MCP

Run the connection doctor from an environment that already contains the three
variables:

```bash
uv run plane-mcp-server doctor
```

The command emits JSON and never includes the token. `connected: true` means
the core API is usable. `pages_api.supported: false` means that the Plane
backend needs the API-token Pages overlay or a compatible upstream release.

The `plane.22lab.dev` backend overlay also exposes project feature settings and
saved Views and project Page image assets to API tokens. The local MCP adds
`view` CRUD, Page asset upload/list/download/delete, and an `automation` facade
for Plane Community Edition's auto-archive and auto-close settings.

## Start manually

```bash
uv run plane-mcp-server stdio
```

Codex normally starts this command itself using the example configuration.
