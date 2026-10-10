import asyncio

import httpx
import pytest

from app.config import Settings
from app.engine import EngineError, Transmission


def settings(tmp_path):
    return Settings(
        "a" * 12, "b" * 32, tmp_path, "http://engine/transmission/rpc", "user", "pass"
    )


def test_rpc_handshake_and_session_rotation(tmp_path):
    async def run():
        engine = Transmission(settings(tmp_path))
        await engine.client.aclose()
        calls = []

        def handler(request):
            calls.append(request)
            if request.headers.get("X-Transmission-Session-Id") != "new-session":
                return httpx.Response(
                    409, headers={"X-Transmission-Session-Id": "new-session"}
                )
            return httpx.Response(
                200, json={"result": "success", "arguments": {"version": "3.00"}}
            )

        engine.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        assert await engine.call("session-get") == {"version": "3.00"}
        assert len(calls) == 2
        assert await engine.call("session-get") == {"version": "3.00"}
        assert len(calls) == 3
        await engine.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401),
        httpx.Response(200, text="bad json"),
        httpx.Response(200, json={"result": "invalid torrent"}),
    ],
)
def test_rpc_failure_is_reported(tmp_path, response):
    async def run():
        engine = Transmission(settings(tmp_path))
        await engine.client.aclose()
        engine.client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: response)
        )
        with pytest.raises(EngineError):
            await engine.call("session-get")
        await engine.close()

    asyncio.run(run())
