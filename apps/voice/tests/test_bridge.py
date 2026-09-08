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
    event = {
        "operationId": id,
        "seq": 1,
        "type": "terminal",
        "operation": {
            "id": id,
            "status": status,
            "output": output if output is not None else {"text": "Done.", "messageId": "m1"},
        },
    }
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=f"data: {json.dumps(event)}\n\n"
    )


async def test_http_contract_preserves_url_payload_and_operation_id():
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(202, json={"operationId": "op-1"})
        return operation()

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
        assert all(str(r.url).endswith("/prefix/v1/operations/op-1/reply") for r in requests[1:])
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


async def test_get_failure_never_reconnects_or_resubmits_audio():
    methods = []

    def handler(request):
        methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(202, json={"operationId": "op-1"})
        raise httpx.ConnectError("offline")

    bridge = bridge_for(handler)
    try:
        with pytest.raises(BridgeError, match="pending"):
            await bridge.answer(TURN)
        assert methods == ["POST", "GET"]
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
        assert paths[-1] == b"/prefix/v1/operations/opaque%2Fwith%3F%23/reply"
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


class ReplyBytes(httpx.AsyncByteStream):
    """In-memory streaming transport; cancellation closes it without cancelling work."""

    def __init__(self, events, release=None, tail=None):
        self.events = events
        self.release = release
        self.tail = tail or []
        self.closed = False

    async def __aiter__(self):
        for event in self.events:
            yield f"data: {json.dumps(event)}\n\n".encode()
        if self.release:
            await self.release.wait()
        for event in self.tail:
            yield f"data: {json.dumps(event)}\n\n".encode()

    async def aclose(self):
        self.closed = True


def text_event(text, seq=1, operation_id="op-1"):
    return {"operationId": operation_id, "seq": seq, "type": "text", "text": text}


def final_event(text="First. Second.", seq=2):
    return {
        "operationId": "op-1",
        "seq": seq,
        "type": "terminal",
        "operation": {
            "id": "op-1",
            "status": "succeeded",
            "output": {"text": text, "messageId": "m1"},
        },
    }


def streaming_bridge(stream, limits=LIMITS):
    methods = []

    def handler(request):
        methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(202, json={"operationId": "op-1"})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    return bridge_for(handler, limits), methods


async def test_incremental_reply_arrives_before_terminal_without_polling_or_final_replay():
    release = asyncio.Event()
    stream = ReplyBytes([text_event("First. ")], release, [final_event()])
    bridge, methods = streaming_bridge(stream)
    reply = bridge.stream_answer(TURN)
    try:
        assert await anext(reply) == "First. "
        assert not release.is_set()
        release.set()
        assert [part async for part in reply] == ["Second."]
        assert methods == ["POST", "GET"]
        assert stream.closed
    finally:
        await reply.aclose()
        await bridge.aclose()


async def test_duplicate_frames_do_not_repeat_speech():
    event = text_event("First. ")
    bridge, methods = streaming_bridge(ReplyBytes([event, event, final_event()]))
    try:
        assert await bridge.answer(TURN) == "First. Second."
        assert methods == ["POST", "GET"]
    finally:
        await bridge.aclose()


@pytest.mark.parametrize("tail", [[], [final_event("Corrected reply.")]])
async def test_loss_or_correction_after_prefix_never_replays_or_resubmits(tail):
    bridge, methods = streaming_bridge(ReplyBytes([text_event("First. "), *tail]))
    reply = bridge.stream_answer(TURN)
    try:
        assert await anext(reply) == "First. "
        with pytest.raises(BridgeError, match="partial"):
            await anext(reply)
        assert [part async for part in bridge.stream_answer(TURN)] == []
        assert methods == ["POST", "GET"]
    finally:
        await reply.aclose()
        await bridge.aclose()


@pytest.mark.parametrize(
    "event",
    [
        text_event("Bad", 2),
        text_event("Bad", operation_id="other"),
        text_event("x" * 65537),
        {"seq": True},
    ],
)
async def test_wrong_identity_gaps_and_size_fail_before_speech(event):
    bridge, _ = streaming_bridge(ReplyBytes([event]))
    try:
        with pytest.raises(BridgeError, match="protocol"):
            await bridge.answer(TURN)
    finally:
        await bridge.aclose()


async def test_interruption_after_partial_closes_stream_without_cancelling_work():
    stream = ReplyBytes([text_event("First. ")], asyncio.Event())
    bridge, methods = streaming_bridge(stream)
    reply = bridge.stream_answer(TURN)
    assert await anext(reply) == "First. "
    await reply.aclose()
    assert stream.closed
    assert methods == ["POST", "GET"]
    await bridge.aclose()


async def test_delayed_receipt_ack_once_and_fast_answer_suppresses_it():
    release = asyncio.Event()
    stream = ReplyBytes([], release, [final_event(seq=1)])
    bridge, methods = streaming_bridge(stream, BridgeLimits(ack_delay=0.01))
    reply = bridge.stream_answer(TURN)
    try:
        assert await asyncio.wait_for(anext(reply), 1) == "Got it.\n\n"
        release.set()
        assert [part async for part in reply] == ["First. Second."]
        assert methods == ["POST", "GET"]
    finally:
        await reply.aclose()
        await bridge.aclose()
    fast, _ = streaming_bridge(ReplyBytes([final_event(seq=1)]))
    try:
        assert [part async for part in fast.stream_answer(TURN)] == ["First. Second."]
    finally:
        await fast.aclose()


async def test_cancel_while_waiting_after_ack_closes_pending_read():
    stream = ReplyBytes([], asyncio.Event())
    bridge, methods = streaming_bridge(stream, BridgeLimits(ack_delay=0.01))
    reply = bridge.stream_answer(TURN)
    assert await asyncio.wait_for(anext(reply), 1) == "Got it.\n\n"
    await reply.aclose()
    assert stream.closed
    assert methods == ["POST", "GET"]
    await bridge.aclose()
