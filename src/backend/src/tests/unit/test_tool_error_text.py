"""Unexpected tool failures must not send exception text to the client (#1122).

A tool result reaches the MCP client and the chat LLM (which may quote it), so
SQL statements, parameters and paths in an exception must stay in the server
log. The expected errors (validation, not found) are unchanged.
"""
import asyncio
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import src.tools as tools_pkg
from src.routes.mcp_routes import MCPHandler
from src.tools.base import (
    TOOL_FAILED,
    TOOL_FAILED_CODE,
    TOOL_FAILED_MESSAGE,
    ToolContext,
    ToolResult,
)
from src.tools.domains import GetDomainTool
from src.tools.unity_catalog import ListCatalogsTool

SECRET = (
    "(psycopg2.errors.UniqueViolation) duplicate key "
    "[SQL: INSERT INTO domains (id, name) VALUES (%(id)s, %(name)s)] "
    "[parameters: {'id': 'p-1', 'name': 'Orders'}]"
)


def _run(coro):
    return asyncio.run(coro)


def _assert_generic(error: str):
    assert error == TOOL_FAILED_MESSAGE
    for leaked in ("SQL", "parameters", "psycopg2", "Error", "Orders"):
        assert leaked not in error


def test_tool_failed_constant_has_code_and_message():
    res = ToolResult(success=False, **TOOL_FAILED)
    assert res.error == TOOL_FAILED_MESSAGE
    assert res.error_code == TOOL_FAILED_CODE == "tool_failed"
    # error_code is additive: the dict keeps "error" and adds the code.
    assert res.to_dict() == {"error": TOOL_FAILED_MESSAGE, "error_code": "tool_failed"}


def test_result_without_code_serializes_as_before():
    assert ToolResult(success=False, error="nope").to_dict() == {"error": "nope"}
    assert ToolResult(success=True, data={"a": 1}).to_dict() == {"a": 1}


def test_tool_hides_exception_text_and_logs_it(caplog):
    ctx = ToolContext(db=MagicMock(), settings=MagicMock())
    ctx.db.query.side_effect = RuntimeError(SECRET)
    with caplog.at_level(logging.ERROR):
        res = _run(GetDomainTool().execute(ctx, domain_id="d-1"))
    assert not res.success
    _assert_generic(res.error)
    assert res.error_code == "tool_failed"
    assert SECRET in caplog.text  # full text stays in the server log


def test_tool_with_extra_data_keeps_it(caplog):
    ctx = ToolContext(db=MagicMock(), settings=MagicMock(), workspace_client=MagicMock())
    ctx.workspace_client.catalogs.list.side_effect = RuntimeError(SECRET)
    with caplog.at_level(logging.ERROR):
        res = _run(ListCatalogsTool().execute(ctx))
    assert not res.success
    _assert_generic(res.error)
    assert SECRET in caplog.text


def test_no_tool_puts_exception_text_in_error():
    """Guard against new sites that echo the exception back to the client."""
    tools_dir = Path(tools_pkg.__file__).parent
    bad = re.compile(r"error=.*(str\(e\)|\{e\}|__name__)")
    offenders = []
    for path in tools_dir.glob("*.py"):
        for no, line in enumerate(path.read_text().splitlines(), 1):
            if bad.search(line) and "logger" not in line:
                offenders.append(f"{path.name}:{no}: {line.strip()}")
    # Expected, user-meaningful error: the SPARQL validator's ValueError.
    # registry.py's catch-all is handled by the health-warning-codes change
    # (#1129), which touches the same lines; drop this exemption once merged.
    allowed = ("semantic_models.py", "registry.py")
    offenders = [o for o in offenders if not o.startswith(allowed)]
    assert offenders == []


def test_mcp_exception_branch_hides_text(caplog):
    app = SimpleNamespace(state=SimpleNamespace(workspace_client=object()))
    request = SimpleNamespace(headers={}, app=app, client=SimpleNamespace(host="127.0.0.1"))
    audit = MagicMock()
    handler = MCPHandler(
        db=None,
        settings=MagicMock(),
        token_info=MagicMock(name="token", created_by="caller@x", scopes=["*"]),
        request=request,
        audit_manager=audit,
        session_id="s-1",
    )

    class Boom:
        required_scope = None

        async def execute(self, ctx, **kwargs):
            raise RuntimeError(SECRET)

    handler._tool_registry = MagicMock()
    handler._tool_registry.get.return_value = Boom()
    handler._create_tool_context = lambda: ToolContext(db=MagicMock(), settings=MagicMock())
    with caplog.at_level(logging.ERROR):
        out = _run(handler._handle_tools_call({"name": "boom", "arguments": {}}))
    assert out["isError"] is True
    _assert_generic(out["content"][0]["text"])
    assert SECRET in caplog.text
