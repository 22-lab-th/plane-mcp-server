"""Server instructions, sent to a client before it lists the tools.

The epic recipe names the tools that do each step, so `test_references.py`
follows every one of them and fails if a name stops resolving.
"""

from __future__ import annotations

SERVER_INSTRUCTIONS = """
## Epics

There are no epic tools — an epic is a work item whose type is named "Epic". Work
items always belong to a project; ask which if one is not named.
1. type = `workitem_type resolve` with project_id and name="Epic" — type.id is the type_id.
2. Create: `workitem create` with project_id, type_id=type.id, name=...
3. List: `workitem list` with project_id and pql='type = "<type id>"'.
4. Read / update / delete / nest: `workitem retrieve` / `workitem update` /
   `workitem delete` by work item id (set parent=<work item id> to nest).
5. List an epic's children: `workitem list` with pql='childOf("<EPIC-IDENTIFIER>")'
   using the epic's human-readable identifier (e.g. "PROJ-12") from `workitem retrieve`.

## 22lab imports and files

Use `atlassian_import` for Confluence/Jira imports into a selected Plane project.
API imports return a background run: present its phase, provisional/final totals,
type counts and failures from `atlassian_import retrieve_run`; follow next_offset for every result.
Keep source_id for sync/recovery and use results[].id for retry_selected.
After a timeout, inspect list_runs before submitting another start. Overwrite
replaces local edits; use it only when the user has authorized that replacement.
Jira sprints map to Cycles. Configure credentials in God Mode or the admin-only
instance tool, without sending secrets in tool arguments.

Use `project_file` for durable files, versions, entity links and verified uploads.
Keep project-file:<id> references in document media rather than expiring download
URLs. File revisions need explicit activation. Local imports/uploads read files
on the MCP server host and require configured allowed roots. The extension tools
use the matching 22lab backend API-token routes and preserve caller permissions.
"""
