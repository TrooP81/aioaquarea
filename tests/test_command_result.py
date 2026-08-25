from aioaquarea.command_result import PanasonicCommandResult


class FakeResponse:
    status = 202
    content_type = "application/json"

    async def json(self):
        return {
            "code": 0,
            "requestId": "request-123",
            "accessToken": {"token": "must-not-be-persisted"},
        }


async def test_command_result_extracts_only_safe_audit_metadata() -> None:
    result = await PanasonicCommandResult.from_response(FakeResponse())

    assert result == PanasonicCommandResult(
        http_status=202,
        response_code=0,
        request_id="request-123",
    )
    assert result.audit_fields() == {
        "panasonic_http_status": 202,
        "panasonic_response_code": 0,
        "panasonic_request_id": "request-123",
    }
    assert "token" not in str(result.audit_fields())


class EmptyResponse:
    status = 204
    content_type = "text/plain"


async def test_command_result_supports_empty_success_response() -> None:
    result = await PanasonicCommandResult.from_response(EmptyResponse())

    assert result.audit_fields() == {"panasonic_http_status": 204}
