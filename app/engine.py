import httpx

FIELDS = [
    "id",
    "hashString",
    "name",
    "status",
    "percentDone",
    "totalSize",
    "leftUntilDone",
    "rateDownload",
    "rateUpload",
    "eta",
    "peersConnected",
    "error",
    "errorString",
    "addedDate",
    "downloadDir",
    "metadataPercentComplete",
    "isStalled",
]


class EngineError(Exception):
    pass


class Transmission:
    def __init__(self, settings):
        self.url = settings.rpc_url
        self.session_id = ""
        self.client = httpx.AsyncClient(
            auth=(settings.rpc_username, settings.rpc_password),
            timeout=15,
            trust_env=False,
        )

    async def call(self, method, arguments=None):
        try:
            for _ in range(3):
                response = await self.client.post(
                    self.url,
                    json={"method": method, "arguments": arguments or {}},
                    headers={"X-Transmission-Session-Id": self.session_id},
                )
                if response.status_code == 409:
                    self.session_id = response.headers.get(
                        "X-Transmission-Session-Id", ""
                    )
                    continue
                response.raise_for_status()
                result = response.json()
                if result.get("result") != "success":
                    raise EngineError(
                        "Transmission: " + str(result.get("result", "unknown error"))
                    )
                return result.get("arguments", {})
        except (httpx.HTTPError, ValueError) as exc:
            raise EngineError(
                "Торрент-сервис недоступен. Проверьте Transmission."
            ) from exc
        raise EngineError("Не удалось установить соединение с Transmission.")

    async def torrents(self, ids=None, files=False):
        args = {"fields": FIELDS + (["files"] if files else [])}
        if ids is not None:
            args["ids"] = ids
        return (await self.call("torrent-get", args))["torrents"]

    async def close(self):
        await self.client.aclose()
