import pytest

from zab.testing.fake_zulip import FakeZulip
from zab.zulip.client import ZulipClient, ZulipError

pytestmark = pytest.mark.integration


async def test_register_send_edit_events_and_queue_loss():
    fake = FakeZulip()
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    queue = await client.register()
    assert fake.requests[0][2]["apply_markdown"] == "false"
    mid = await client.send(1, "topic", "hello")
    await client.edit(mid, "edited")
    assert fake.messages[mid]["content"] == "edited"
    events = await client.events(queue["queue_id"], queue["last_event_id"])
    assert events[0]["message"]["content"] == "hello"
    fake.lose_queues()
    with pytest.raises(ZulipError) as exc:
        await client.events(queue["queue_id"], 0)
    assert exc.value.code == "BAD_EVENT_QUEUE_ID"
    await client.close()


async def test_limits_and_429_are_visible_to_poster():
    fake = FakeZulip()
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    await client.register()
    with pytest.raises(ValueError, match="server limit"):
        await client.send(1, "topic", "x" * 10001)
    await client.send(1, "x" * 70, "short")
    assert len(fake.messages[1]["subject"]) == 60
    fake.rate_limit_once(3)
    with pytest.raises(ZulipError) as exc:
        await client.send(1, "topic", "hello")
    assert exc.value.status == 429
    assert exc.value.retry_after == 3
    assert len(fake.messages) == 1
    await client.close()


async def test_credentials_do_not_follow_arbitrary_urls():
    client = ZulipClient("https://zulip.example", "bot", "decoy")
    with pytest.raises(ValueError, match="relative"):
        await client.request("GET", "https://elsewhere.example")
    await client.close()


async def test_fake_edit_and_reaction_deliver_events():
    fake = FakeZulip()
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    queue = await client.register()
    mid = await client.send(1, "topic", "hello")
    await client.request("PATCH", f"messages/{mid}", topic="renamed", stream_id=2)
    await client.request(
        "POST",
        f"messages/{mid}/reactions",
        emoji_name="stop_sign",
        emoji_code="1f6d1",
        reaction_type="unicode_emoji",
    )
    events = await client.events(queue["queue_id"], queue["last_event_id"])
    assert [e["type"] for e in events] == ["message", "update_message", "reaction"]
    assert events[1]["new_stream_id"] == 2
    assert events[1]["subject"] == "renamed"
    assert events[2]["message_id"] == mid
    await client.close()


async def test_attachment_redirects_never_receive_bot_credentials():
    import httpx

    requests = []

    async def handle(request):
        requests.append(request)
        assert "Authorization" not in request.headers
        if request.url.host == "zulip.example":
            return httpx.Response(302, headers={"Location": "https://storage.example/signed"})
        return httpx.Response(200, content=b"data", headers={"Content-Type": "text/csv"})

    client = ZulipClient(
        "https://zulip.example", "bot", "decoy", transport=httpx.MockTransport(handle)
    )
    try:
        assert await client.download("/user_uploads/temporary/decoy", 4) == (b"data", "text/csv")
        assert len(requests) == 2
        with pytest.raises(ValueError, match="HTTPS"):
            await client.download("http://storage.example/plain", 10)
    finally:
        await client.close()


@pytest.mark.parametrize("length_header", [True, False])
async def test_download_enforces_size_with_or_without_content_length(length_header):
    import httpx

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"abcd"
            yield b"efgh"

    async def handle(request):
        return httpx.Response(
            200, stream=Stream(), headers={"Content-Length": "8"} if length_header else {}
        )

    client = ZulipClient(
        "https://zulip.example", "bot", "decoy", transport=httpx.MockTransport(handle)
    )
    try:
        with pytest.raises(ValueError, match="size limit"):
            await client.download("/signed", 6)
    finally:
        await client.close()


@pytest.mark.parametrize("filename", ["results..csv", "report%3A2026.csv"])
async def test_endpoint_validation_accepts_filenames_but_rejects_traversal(filename):
    from urllib.parse import unquote

    fake = FakeZulip()
    fake.uploads[f"1/a/{unquote(filename)}"] = (b"data", "text/csv")
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    try:
        assert "url" in await client.request("GET", f"user_uploads/1/a/{filename}")
        for path in ("user_uploads/1/../secret", "user_uploads/1/%2e%2e/secret", "%2foutside"):
            with pytest.raises(ValueError, match="relative"):
                await client.request("GET", path)
        assert len(fake.requests) == 1
    finally:
        await client.close()


async def test_every_client_shares_one_tls_context(monkeypatch):
    """#717: httpx builds a TLS context per client, about 0.7 MB each with its root
    certificates; the process builds one, for each client's two (the API's and the downloads')."""
    import ssl

    from zab.zulip import client as module

    monkeypatch.setattr(module, "_TLS", None)
    built, create = [], ssl.create_default_context

    def counting(*args, **kwargs):
        built.append(args or kwargs)
        return create(*args, **kwargs)

    monkeypatch.setattr(ssl, "create_default_context", counting)
    clients = [ZulipClient("https://zulip.example", f"bot{n}", "decoy") for n in range(3)]
    assert len(built) == 1 and module.tls() is module.tls()
    context = module.tls()  # still httpx's default: certificates and host names checked
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
    for client in clients:
        await client.close()
