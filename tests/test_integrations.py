from pydantic import SecretStr

from home_kitchen.integrations import (
    AIIntegration,
    CookidooIntegration,
    OdaIntegration,
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


def test_cookidoo_is_explicitly_unavailable_without_supported_auth_or_capabilities():
    result = CookidooIntegration().describe("member-1")

    assert result.status == "unavailable"
    assert result.auth_mode is None
    assert result.supported_auth_modes == []
    assert result.capabilities == []
    assert "official supported third-party connection path" in result.detail
    assert "native" not in result.detail.lower()
