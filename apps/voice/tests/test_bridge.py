import asyncio
import json

import httpx
import pytest

from orchestrator_voice.bridge import BridgeError, BridgeLimits, ControlBridge, Turn

TURN = Turn("turn-1", "Check worker status", "session:opaque/value")
LIMITS = BridgeLimits(
    request_timeout=0.1, submission_timeout=0.2, poll_timeout=0.1, poll_interval=0.001
)


def bridge_for(handler, limits=LIMITS):
    return ControlBridge(
        httpx.AsyncClient(
            base_url="http://control.test/prefix/",
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ),
        limits,
    )


def operation(status="succeeded", output=None, id="op-1"):
    return httpx.Response(
        200,
        json={
            "operation": {
                "id": id,
                "status": status,
                "output": output if output is not None else {"text": "Done.", "messageId": "m1"},
            }
        },
    )


async def test_http_contract_preserves_url_payload_and_operation_id():
    requests = []
    states = iter(["queued", "running", "succeeded"])

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(202, json={"operationId": "op-1"})
        return operation(next(states))

    bridge = bridge_for(handler)
    try:
        assert await bridge.answer(TURN) == "Done."
        assert json.loads(requests[0].content) == {
            "id": "turn-1",
            "text": "Check worker status",
            "conversationId": "session:opaque/value",
            "source": "voice",
        }
        assert str(requests[0].url) == "http://control.test/prefix/v1/chat"
        assert all(str(r.url).endswith("/prefix/v1/operations/op-1") for r in requests[1:])
    finally:
        await bridge.aclose()


@pytest.mark.parametrize("failure", ["transport", "500", "429", "invalid_json", "missing_id"])
async def test_unknown_post_retries_identical_id_and_payload(failure):
    payloads = []

    def handler(request):
        if request.method == "GET":
            return operation()
        payloads.append(request.content)
        if len(payloads) == 1:
            if failure == "transport":
                raise httpx.ReadError("ack lost")
            if failure == "invalid_json":
                return httpx.Response(202, text="not json")
            if failure == "missing_id":
                return httpx.Response(202, json={})
            return httpx.Response(int(failure))
        return httpx.Response(202, json={"operationId": "op-1"})

    bridge = bridge_for(handler)
    try:
        assert await bridge.answer(TURN) == "Done."
        assert len(payloads) == 2
        assert payloads[0] == payloads[1]
    finally:
        await bridge.aclose()


@pytest.mark.parametrize("status", [400, 401, 403, 409, 422, 302])
async def test_definite_rejection_never_retries_or_follows_redirect(status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "http://elsewhere.test"})

    bridge = bridge_for(handler)
    try:
        with pytest.raises(BridgeError) as caught:
            await bridge.answer(TURN)
        assert caught.value.kind == "rejected"
        assert len(requests) == 1
    finally:
        await bridge.aclose()


async def test_exhausted_post_is_unknown_not_failed():
    ids = []

    def handler(request):
        ids.append(json.loads(request.content)["id"])
        raise httpx.ReadTimeout("possibly committed")

    bridge = bridge_for(handler)
    try:
        with pytest.raises(BridgeError) as caught:
            await bridge.answer(TURN)
        assert caught.value.kind == "unknown_submission"
        assert ids == [TURN.id] * 3
        assert "may still run" in caught.value.spoken_text
    finally:
        await bridge.aclose()


async def test_cancellation_during_unknown_post_finishes_same_id_reconciliation():
    entered = asyncio.Event()
    release = asyncio.Event()
    payloads = []
    gets = []

    async def handler(request):
        if request.method == "GET":
            gets.append(request)
            return operation()
        payloads.append(request.content)
        if len(payloads) == 1:
            entered.set()
            await release.wait()
            raise httpx.ReadError("ack lost after commit")
        return httpx.Response(202, json={"operationId": "op-1"})

    bridge = bridge_for(handler)
    consumer = asyncio.create_task(bridge.answer(TURN))
    await entered.wait()
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    release.set()
    await bridge.aclose()  # Waits for bounded background submission, not polling.
    assert len(payloads) == 2
    assert payloads[0] == payloads[1]
    assert gets == []


async def test_interruption_cancels_poll_not_accepted_operation():
    polling = asyncio.Event()
    poll_cancelled = asyncio.Event()
    methods = []
    durable = {}

    async def handler(request):
        methods.append(request.method)
        if request.method == "POST":
            durable["op-1"] = "running"
            return httpx.Response(202, json={"operationId": "op-1"})
        polling.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            poll_cancelled.set()
            raise

    bridge = bridge_for(handler)
    task = asyncio.create_task(bridge.answer(TURN))
    await polling.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert poll_cancelled.is_set()
    assert durable == {"op-1": "running"}
    assert methods == ["POST", "GET"]
    await bridge.aclose()


@pytest.mark.parametrize("status", ["failed", "uncertain", "bogus"])
async def test_terminal_failures_do_not_resubmit(status):
    methods = []

    def handler(request):
        methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(202, json={"operationId": "op-1"})
        return operation(status)

    bridge = bridge_for(handler)
    try:
        with pytest.raises(BridgeError) as caught:
            await bridge.answer(TURN)
        assert caught.value.kind == ("protocol" if status == "bogus" else status)
        assert caught.value.operation_id == "op-1"
        assert methods == ["POST", "GET"]
    finally:
        await bridge.aclose()


@pytest.mark.parametrize("body", [[], {}, {"operation": []}, {"operation": {"id": "wrong"}}])
async def test_malformed_get_is_honest_protocol_error(body):
    def handler(request):
        if request.method == "POST":
            return httpx.Response(202, json={"operationId": "op-1"})
        return httpx.Response(200, json=body)

    bridge = bridge_for(handler)
    try:
        with pytest.raises(BridgeError, match="protocol"):
            await bridge.answer(TURN)
    finally:
        await bridge.aclose()


@pytest.mark.parametrize("output", [{"text": 123, "messageId": "m"}, {"text": "done"}])
async def test_invalid_success_output_is_not_spoken(output):
    bridge = bridge_for(
        lambda r: (
            httpx.Response(202, json={"operationId": "op-1"})
            if r.method == "POST"
            else operation(output=output)
        )
    )
    try:
        with pytest.raises(BridgeError, match="protocol"):
            await bridge.answer(TURN)
    finally:
        await bridge.aclose()


async def test_get_reconnects_independently_without_resubmitting():
    methods = []
    failures = iter([404, 503, 429, "connection", "success"])

    def handler(request):
        methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(202, json={"operationId": "op-1"})
        failure = next(failures)
        if failure == "connection":
            raise httpx.ConnectError("offline")
        if failure == "success":
            return operation()
        assert isinstance(failure, int)
        return httpx.Response(failure)

    bridge = bridge_for(handler)
    try:
        assert await bridge.answer(TURN) == "Done."
        assert methods.count("POST") == 1
        assert methods.count("GET") == 5
    finally:
        await bridge.aclose()


@pytest.mark.parametrize("stage,kind", [("POST", "unknown_submission"), ("GET", "pending")])
async def test_hard_deadline_bounds_hung_transport(stage, kind):
    async def handler(request):
        if request.method == stage:
            await asyncio.Event().wait()
        return httpx.Response(202, json={"operationId": "op-1"})

    bridge = bridge_for(handler, BridgeLimits(submission_timeout=0.02, poll_timeout=0.02))
    try:
        with pytest.raises(BridgeError) as caught:
            await bridge.answer(TURN)
        assert caught.value.kind == kind
    finally:
        await bridge.aclose()


async def test_operation_id_is_escaped_as_one_path_segment() -> None:
    paths = []

    def handler(request):
        paths.append(request.url.raw_path)
        if request.method == "POST":
            return httpx.Response(202, json={"operationId": "opaque/with?#"})
        return operation(id="opaque/with?#")

    bridge = bridge_for(handler)
    try:
        assert await bridge.answer(TURN) == "Done."
        assert paths[-1] == b"/prefix/v1/operations/opaque%2Fwith%3F%23"
    finally:
        await bridge.aclose()


async def test_rejection_after_lost_ack_remains_unknown():
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadError("may already be accepted")
        return httpx.Response(401)

    bridge = bridge_for(handler)
    try:
        with pytest.raises(BridgeError) as caught:
            await bridge.answer(TURN)
        assert caught.value.kind == "unknown_submission"
        assert attempts == 2
    finally:
        await bridge.aclose()
