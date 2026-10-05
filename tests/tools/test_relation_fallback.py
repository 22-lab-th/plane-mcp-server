"""workitem_relation on a Plane Community server.

Community Plane has no `/dependencies/`, `/work-item-relations/` or
`/work-item-relation-definitions/` routes (404). It has one `/relations/` route that
carries the built-in relation types. The tool must fall back to it instead of failing
every action with 404, and must still surface any other error.
"""

from __future__ import annotations

import pytest
from plane.errors.errors import HttpError

from plane_mcp.tools import workitem_relation as mod

NOT_FOUND = HttpError("Not Found", 404)
SERVER_ERROR = HttpError("boom", 500)


class _Dump:
    def __init__(self, data):
        self.data = data

    def model_dump(self):
        return self.data


class _Group:
    """Records calls; raises `error` for every method when set, else returns `value`."""

    def __init__(self, calls, name, error=None, value=None):
        self._calls, self._name, self._error, self._value = calls, name, error, value

    def __getattr__(self, method):
        def call(**kwargs):
            self._calls.append((f"{self._name}.{method}", kwargs))
            if self._error:
                raise self._error
            return self._value

        return call


class FakeClient:
    def __init__(self, *, deps_error=NOT_FOUND, custom_error=NOT_FOUND, defs_error=NOT_FOUND, native_error=None):
        self.calls: list[tuple[str, dict]] = []
        native = _Dump({"blocked_by": [], "blocking": ["w-2"], "relates_to": []})
        self.work_items = type("WI", (), {})()
        self.work_items.dependencies = _Group(self.calls, "dependencies", deps_error)
        self.work_items.custom_relations = _Group(self.calls, "custom_relations", custom_error)
        self.work_items.relations = _Group(self.calls, "relations", native_error, native)
        self.work_item_relation_definitions = _Group(self.calls, "definitions", defs_error)

    def names(self):
        return [name for name, _ in self.calls]


@pytest.fixture
def run(registered, monkeypatch):
    def _run(client, **args):
        monkeypatch.setattr(mod, "get_plane_client_context", lambda: (client, "ws"))
        return registered["workitem_relation"].fn(**args)

    return _run


def test_list_falls_back_to_native_relations(run):
    client = FakeClient()
    result = run(client, action="list", project_id="p", workitem_id="w")
    assert result["dependencies"]["blocking"] == ["w-2"]
    assert result["custom"] == {}
    assert "relations.list" in client.names()


def test_create_dependency_falls_back_to_native_relations(run):
    client = FakeClient()
    run(client, action="create", project_id="p", workitem_id="w", workitem_ids=["w-2"], relation_type="blocking")
    name, kwargs = client.calls[-1]
    assert name == "relations.create"
    assert kwargs["data"].relation_type == "blocking" and kwargs["data"].issues == ["w-2"]


def test_list_definitions_is_empty_when_the_server_has_none(run):
    result = run(FakeClient(), action="list_definitions")
    assert result["custom_definitions"] == []
    assert "blocking" in result["built_in_dependencies"]


def test_remove_dependency_reports_when_the_server_cannot_remove(run):
    client = FakeClient(native_error=NOT_FOUND)
    result = run(
        client, action="delete", project_id="p", workitem_id="w", related_workitem_id="w-2", is_dependency=True
    )
    assert isinstance(result, str) and result.startswith("Error:") and "web UI" in result


def test_other_errors_are_not_swallowed(run):
    client = FakeClient(deps_error=SERVER_ERROR)
    with pytest.raises(HttpError):
        run(client, action="list", project_id="p", workitem_id="w")
    assert "relations.list" not in client.names()


def test_native_path_is_not_used_when_dependencies_work(run):
    client = FakeClient(deps_error=None)
    client.work_items.dependencies = _Group(client.calls, "dependencies", None, _Dump({"blocking": []}))
    client.work_items.custom_relations = _Group(client.calls, "custom_relations", None, {})
    run(client, action="list", project_id="p", workitem_id="w")
    assert "relations.list" not in client.names()
