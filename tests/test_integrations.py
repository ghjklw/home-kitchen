from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import SecretStr

from home_kitchen.integrations import (
    AIIntegration,
    CookidooIntegration,
    CoopIntegration,
    OdaIntegration,
    Rema1000Integration,
    TrumfIntegration,
)


class OdaStatus:
    def __init__(self, result):
        self.result = result
        self.actors = []

    def status(self, actor_id):
        self.actors.append(actor_id)
        return self.result


def test_oda_connection_status_is_actor_scoped():
    oda = OdaStatus({"configured": True, "connected": True})

    result = OdaIntegration(oda).describe("household-member-1")

    assert oda.actors == ["household-member-1"]
    assert result.status == "connected"
    assert result.auth_mode == "delegated_user_pkce"
    assert result.supported_auth_modes == ["delegated_user_pkce"]


def test_oda_unconfigured_and_disconnected_statuses_do_not_claim_connection():
    for state, expected in [
        ({"configured": False, "connected": False}, "not_configured"),
        ({"configured": True, "connected": False}, "configured"),
    ]:
        result = OdaIntegration(OdaStatus(state)).describe(None)

        assert result.status == expected
        assert result.auth_mode is None


def test_ai_reports_operator_configuration_without_connection_claim_or_secret():
    secret = "anthropic-secret-do-not-serialize"
    settings = type(
        "AISettings",
        (),
        {
            "anthropic_workload_identity": False,
            "anthropic_api_key": SecretStr(secret),
            "ai_configured": True,
        },
    )()

    result = AIIntegration(settings).describe("member-1")
    serialized = result.model_dump_json()

    assert result.status == "configured"
    assert result.auth_mode == "api_key"
    assert result.supported_auth_modes == ["federation", "api_key"]
    assert "connected" not in serialized
    assert secret not in serialized
    assert "secret" not in serialized
    assert "successful model call" in result.detail


def test_ai_federation_configuration_is_reported_without_credentials():
    settings = type(
        "AISettings",
        (),
        {
            "anthropic_workload_identity": True,
            "anthropic_api_key": SecretStr("unused-secret"),
            "ai_configured": True,
        },
    )()

    result = AIIntegration(settings).describe(None)

    assert result.auth_mode == "federation"
    assert result.status == "configured"
    assert "unused-secret" not in result.model_dump_json()


def test_unconfigured_ai_does_not_report_an_auth_mode():
    settings = type(
        "AISettings",
        (),
        {
            "anthropic_workload_identity": False,
            "anthropic_api_key": None,
            "ai_configured": False,
        },
    )()

    result = AIIntegration(settings).describe(None)

    assert result.status == "not_configured"
    assert result.auth_mode is None


def test_cookidoo_unconnected_reports_not_configured_with_supported_modes():
    result = CookidooIntegration().describe("member-1")

    assert result.status == "not_configured"
    assert result.auth_mode is None
    assert result.supported_auth_modes == ["oauth_password", "session_cookie", "credentials"]
    assert "recipe_import" in result.capabilities
    assert "Cookidoo" in result.name


def test_connector_integrations_unconnected_report_not_configured():
    rema = Rema1000Integration().describe("actor-1")
    assert rema.id == "rema1000"
    assert rema.status == "not_configured"
    assert rema.supported_auth_modes == ["oauth_pkce", "session_token"]
    assert "purchase_history" in rema.capabilities

    coop = CoopIntegration().describe("actor-1")
    assert coop.id == "coop"
    assert coop.status == "not_configured"
    assert coop.supported_auth_modes == ["oauth_pkce", "session_token", "session_cookie"]

    trumf = TrumfIntegration().describe("actor-1")
    assert trumf.id == "trumf"
    assert trumf.status == "not_configured"
    assert trumf.supported_auth_modes == ["interactive_otp", "session_cookie"]


def test_connector_integrations_connected_and_expired_states():
    class MockConnectorStatus:
        def __init__(self, auth_mode, expires_at, is_expired):
            self.auth_mode = auth_mode
            self.expires_at = expires_at
            self.is_expired = is_expired

    class MockStore:
        def __init__(self, statuses):
            self.statuses = statuses

        def get_connection_metadata(self, household_id, provider_id):
            return self.statuses.get(provider_id)

    future = datetime.now(UTC) + timedelta(days=30)
    past = datetime.now(UTC) - timedelta(days=1)
    household_id = uuid4()

    store = MockStore(
        {
            "rema1000": MockConnectorStatus("session_token", future, False),
            "trumf": MockConnectorStatus("session_cookie", past, True),
        }
    )

    rema = Rema1000Integration(store, household_id).describe("actor-1")
    assert rema.status == "connected"
    assert rema.auth_mode == "session_token"
    assert rema.expires_at == future
    assert rema.expiry_prompt is None

    trumf = TrumfIntegration(store, household_id).describe("actor-1")
    assert trumf.status == "expired"
    assert trumf.auth_mode == "session_cookie"
    assert trumf.expires_at == past
    assert "utløpt" in trumf.expiry_prompt.lower()
