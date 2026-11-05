"""
tests/test_postcall_dispatcher.py

Regression guards for product/postcall/dispatcher.dispatch_timeline_entry_write.

All tests mock at the boundary: HubSpotWriter / get_gainsight_connector
are patched at their import sites inside the dispatcher.

Invariants under test:
  1. HubSpot success — writer.log_note called with the expected args;
     return dict has status=written, external_system_id, crm_type.
  2. HubSpot 5xx — PostCallDispatchError(error_type=network) raised.
  3. HubSpot 401 — PostCallDispatchError(error_type=auth) raised.
  4. Gainsight success — connector.create_timeline_activity called;
     external_system_id=activity_id.
  5. Gainsight connection failure — PostCallDispatchError(error_type=network).
  6. Gainsight tool-not-found — PostCallDispatchError(error_type=invalid_payload).
  7. Unknown crm_type — PostCallDispatchError(invalid_payload) raised
     before any writer call.
  8. Missing required fields — PostCallDispatchError(invalid_payload) raised
     before any writer call.

Standalone-runnable:
    python3 tests/test_postcall_dispatcher.py
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product.connectors.gainsight import GainsightToolNotFoundError
from product.postcall.dispatcher import dispatch_timeline_entry_write
from product.postcall.events import (
    ERROR_TYPE_AUTH,
    ERROR_TYPE_INVALID_PAYLOAD,
    ERROR_TYPE_NETWORK,
    PostCallDispatchError,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _dispatch_kwargs(
    *,
    crm_type: str = "hubspot",
    account_crm_id: str = "hs-company-55",
    csm_id: str = "sarah_c",
    account_name: str = "Meridian Health",
    final_edited_text: str = "Met with Marcus — renewal committed, SSO blocker open.",
) -> dict:
    return {
        "csm_id": csm_id,
        "account_name": account_name,
        "account_crm_id": account_crm_id,
        "crm_type": crm_type,
        "final_edited_text": final_edited_text,
    }


def _mock_hubspot_writer(log_note_return=None, log_note_exc=None):
    writer_instance = MagicMock(name="hubspot_writer_instance")
    if log_note_exc is not None:
        writer_instance.log_note.side_effect = log_note_exc
    else:
        writer_instance.log_note.return_value = log_note_return or {
            "status": "created",
            "note_id": "hs-note-12345",
        }
    return patch(
        "product.connectors.hubspot_write.HubSpotWriter",
        return_value=writer_instance,
    ), writer_instance


def _mock_gainsight_connector(create_return=None, create_exc=None):
    conn = MagicMock(name="gainsight_connector_instance")
    if create_exc is not None:
        conn.create_timeline_activity.side_effect = create_exc
    else:
        conn.create_timeline_activity.return_value = create_return or {
            "status": "created",
            "activity_id": "gs-act-78901",
        }
    return patch(
        "product.connectors.gainsight.get_gainsight_connector",
        return_value=conn,
    ), conn


def _fake_http_error(status: int, message: str = "boom"):
    exc = RuntimeError(message)
    resp = MagicMock()
    resp.status_code = status
    exc.response = resp
    return exc


# ── Tests ────────────────────────────────────────────────────────────────────


def test_hubspot_success_returns_result():
    writer_patch, writer = _mock_hubspot_writer(
        log_note_return={"status": "created", "note_id": "hs-note-12345"},
    )
    with writer_patch:
        result = dispatch_timeline_entry_write(**_dispatch_kwargs(crm_type="hubspot"))

    assert result == {
        "status": "written",
        "external_system_id": "hs-note-12345",
        "crm_type": "hubspot",
    }
    writer.log_note.assert_called_once_with(
        company_id="hs-company-55",
        note_body="Met with Marcus — renewal committed, SSO blocker open.",
    )
    print("✓ HubSpot success: log_note called with expected args, result dict correct")


def test_hubspot_5xx_raises_network_error_type():
    writer_patch, _ = _mock_hubspot_writer(log_note_exc=_fake_http_error(503, "bad gateway"))
    with writer_patch:
        with pytest.raises(PostCallDispatchError) as exc_info:
            dispatch_timeline_entry_write(**_dispatch_kwargs(crm_type="hubspot"))

    assert exc_info.value.error_type == ERROR_TYPE_NETWORK
    assert "bad gateway" in str(exc_info.value)
    print("✓ HubSpot 5xx: PostCallDispatchError(error_type=network) raised")


def test_hubspot_401_raises_auth_error_type():
    writer_patch, _ = _mock_hubspot_writer(log_note_exc=_fake_http_error(401, "unauthorized"))
    with writer_patch:
        with pytest.raises(PostCallDispatchError) as exc_info:
            dispatch_timeline_entry_write(**_dispatch_kwargs(crm_type="hubspot"))

    assert exc_info.value.error_type == ERROR_TYPE_AUTH
    print("✓ HubSpot 401: PostCallDispatchError(error_type=auth)")


def test_gainsight_success_returns_result():
    conn_patch, conn = _mock_gainsight_connector()
    with conn_patch:
        result = dispatch_timeline_entry_write(
            **_dispatch_kwargs(crm_type="gainsight", account_crm_id="gs-company-001"),
        )

    assert result == {
        "status": "written",
        "external_system_id": "gs-act-78901",
        "crm_type": "gainsight",
    }
    conn.create_timeline_activity.assert_called_once_with(
        account_id="gs-company-001",
        account_name="Meridian Health",
        notes="Met with Marcus — renewal committed, SSO blocker open.",
    )
    print("✓ Gainsight success: create_timeline_activity called, result dict correct")


def test_gainsight_connection_failure_raises_network():
    conn_patch, _ = _mock_gainsight_connector(
        create_exc=ConnectionError("MCP transport failure"),
    )
    with conn_patch:
        with pytest.raises(PostCallDispatchError) as exc_info:
            dispatch_timeline_entry_write(**_dispatch_kwargs(crm_type="gainsight"))

    assert exc_info.value.error_type == ERROR_TYPE_NETWORK
    print("✓ Gainsight ConnectionError: PostCallDispatchError(error_type=network)")


def test_gainsight_tool_not_found_raises_invalid_payload():
    """_find_tool failure → invalid_payload (code-side bug, don't retry)."""
    conn_patch, _ = _mock_gainsight_connector(
        create_exc=GainsightToolNotFoundError(
            "no MCP tool matching timeline-activity create keywords; available tools=[]"
        ),
    )
    with conn_patch:
        with pytest.raises(PostCallDispatchError) as exc_info:
            dispatch_timeline_entry_write(**_dispatch_kwargs(crm_type="gainsight"))

    assert exc_info.value.error_type == ERROR_TYPE_INVALID_PAYLOAD
    assert "Gainsight write tool not discovered" in str(exc_info.value)
    print("✓ Gainsight tool-not-found: PostCallDispatchError(error_type=invalid_payload)")


def test_unknown_crm_type_raises_without_any_writer_call():
    writer_patch, writer = _mock_hubspot_writer()
    conn_patch, conn = _mock_gainsight_connector()
    with writer_patch, conn_patch:
        with pytest.raises(PostCallDispatchError) as exc_info:
            dispatch_timeline_entry_write(**_dispatch_kwargs(crm_type="salesforce"))

    assert exc_info.value.error_type == ERROR_TYPE_INVALID_PAYLOAD
    assert writer.log_note.call_count == 0
    assert conn.create_timeline_activity.call_count == 0
    print("✓ unknown crm_type: raises invalid_payload pre-flight, no writer call")


def test_missing_required_fields_raises_without_any_writer_call():
    writer_patch, writer = _mock_hubspot_writer()
    with writer_patch:
        with pytest.raises(PostCallDispatchError) as exc_info:
            dispatch_timeline_entry_write(
                **_dispatch_kwargs(crm_type="hubspot", account_crm_id=""),
            )

    assert exc_info.value.error_type == ERROR_TYPE_INVALID_PAYLOAD
    assert "account_crm_id" in str(exc_info.value)
    assert writer.log_note.call_count == 0
    print("✓ missing account_crm_id: raises invalid_payload, no writer call")


def main():
    """Standalone runner — matches the pattern in sibling test files."""
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
