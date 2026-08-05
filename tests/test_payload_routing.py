"""Regression tests for sharing-layer payload routing.

A real Android phone test exposed a bug (now fixed) where sharing-layer
control frames sent as BYTES payloads were misdelivered as text shares.
InboundConnection must only treat a BYTES payload as a text share when its
payload_id was declared as TextMetadata in the INTRODUCTION frame; any other
BYTES payload (progress updates, cancel, or anything else) is a sharing-layer
control frame and must never reach on_text.

These tests drive InboundConnection over a real localhost TCP connection.
The "sender" side is hand-rolled directly on top of Ukey2Client + D2DCipher
(borrowing the exact frame-building code from quickshare.core.connection) so
we can inject arbitrary/malformed/duplicate/interleaved payloads that the
real OutboundConnection would never produce.

Run:  .venv/bin/python -m tests.test_payload_routing
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
import sys
import tempfile
from pathlib import Path

from quickshare.core.connection import (CHUNK_SIZE, Events, InboundConnection,
                                        build_endpoint_info, read_frame,
                                        write_frame)
from quickshare.core.crypto import D2DCipher, Ukey2Client
from quickshare.proto import offline_wire_formats_pb2 as ow
from quickshare.proto import wire_format_pb2 as wf


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rand_payload_id() -> int:
    return random.randint(-2**63, 2**63 - 1)


# ---------------------------------------------------------------- fake sender

class FakeSender:
    """Hand-rolled sender: drives the wire protocol manually so tests can
    inject frames a well-behaved OutboundConnection never would."""

    def __init__(self, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.cipher: D2DCipher | None = None
        self.pin: str | None = None

    async def handshake(self, device_name: str = "fake-sender") -> None:
        req = ow.OfflineFrame()
        req.version = ow.OfflineFrame.V1
        req.v1.type = ow.V1Frame.CONNECTION_REQUEST
        cr = req.v1.connection_request
        cr.endpoint_id = "TEST"
        cr.endpoint_name = device_name
        cr.endpoint_info = build_endpoint_info(device_name)
        cr.mediums.append(ow.ConnectionRequestFrame.WIFI_LAN)
        write_frame(self.writer, req.SerializeToString())
        await self.writer.drain()

        client = Ukey2Client()
        write_frame(self.writer, client.client_init())
        await self.writer.drain()
        m2 = await read_frame(self.reader)
        m3, keys = client.handle_server_init(m2)
        write_frame(self.writer, m3)
        await self.writer.drain()
        self.cipher = D2DCipher(keys)
        self.pin = keys.pin

        resp = ow.OfflineFrame()
        resp.version = ow.OfflineFrame.V1
        resp.v1.type = ow.V1Frame.CONNECTION_RESPONSE
        resp.v1.connection_response.status = 0
        resp.v1.connection_response.response = ow.ConnectionResponseFrame.ACCEPT
        resp.v1.connection_response.os_info.type = ow.OsInfo.LINUX
        write_frame(self.writer, resp.SerializeToString())
        await self.writer.drain()
        peer_resp = ow.OfflineFrame()
        peer_resp.ParseFromString(await read_frame(self.reader))
        assert peer_resp.v1.type == ow.V1Frame.CONNECTION_RESPONSE
        assert (peer_resp.v1.connection_response.response ==
                ow.ConnectionResponseFrame.ACCEPT)

        # Paired-key dance (mirrors OutboundConnection._handshake).
        pke = wf.Frame()
        pke.version = wf.Frame.V1
        pke.v1.type = wf.V1Frame.PAIRED_KEY_ENCRYPTION
        pke.v1.paired_key_encryption.signed_data = os.urandom(72)
        pke.v1.paired_key_encryption.secret_id_hash = os.urandom(6)
        await self.send_sharing_frame(pke)
        theirs = await self.recv_sharing_frame()
        assert theirs.v1.type == wf.V1Frame.PAIRED_KEY_ENCRYPTION

        pkr = wf.Frame()
        pkr.version = wf.Frame.V1
        pkr.v1.type = wf.V1Frame.PAIRED_KEY_RESULT
        pkr.v1.paired_key_result.status = wf.PairedKeyResultFrame.UNABLE
        await self.send_sharing_frame(pkr)
        their_result = await self.recv_sharing_frame()
        assert their_result.v1.type == wf.V1Frame.PAIRED_KEY_RESULT

    async def send_encrypted(self, frame: ow.OfflineFrame) -> None:
        assert self.cipher is not None
        write_frame(self.writer, self.cipher.encrypt(frame.SerializeToString()))
        await self.writer.drain()

    async def recv_encrypted(self) -> ow.OfflineFrame:
        assert self.cipher is not None
        while True:
            raw = await asyncio.wait_for(read_frame(self.reader), 10)
            frame = ow.OfflineFrame()
            frame.ParseFromString(self.cipher.decrypt(raw))
            if frame.v1.type == ow.V1Frame.KEEP_ALIVE:
                continue
            return frame

    async def send_sharing_frame(self, sharing: wf.Frame) -> None:
        body = sharing.SerializeToString()
        payload_id = rand_payload_id()

        first = ow.OfflineFrame()
        first.version = ow.OfflineFrame.V1
        first.v1.type = ow.V1Frame.PAYLOAD_TRANSFER
        pt = first.v1.payload_transfer
        pt.packet_type = ow.PayloadTransferFrame.DATA
        pt.payload_header.id = payload_id
        pt.payload_header.type = ow.PayloadTransferFrame.PayloadHeader.BYTES
        pt.payload_header.total_size = len(body)
        pt.payload_chunk.offset = 0
        pt.payload_chunk.body = body
        await self.send_encrypted(first)

        last = ow.OfflineFrame()
        last.CopyFrom(first)
        lpt = last.v1.payload_transfer
        lpt.payload_chunk.offset = len(body)
        lpt.payload_chunk.flags = ow.PayloadTransferFrame.PayloadChunk.LAST_CHUNK
        lpt.payload_chunk.ClearField("body")
        await self.send_encrypted(last)

    async def send_bytes_payload(self, payload_id: int, body: bytes) -> None:
        """Send an arbitrary BYTES payload mid-transfer, DATA + LAST_CHUNK,
        exactly like send_sharing_frame but with a caller-chosen payload_id
        (so it can collide -- or not -- with a declared text payload_id)."""
        first = ow.OfflineFrame()
        first.version = ow.OfflineFrame.V1
        first.v1.type = ow.V1Frame.PAYLOAD_TRANSFER
        pt = first.v1.payload_transfer
        pt.packet_type = ow.PayloadTransferFrame.DATA
        pt.payload_header.id = payload_id
        pt.payload_header.type = ow.PayloadTransferFrame.PayloadHeader.BYTES
        pt.payload_header.total_size = len(body)
        pt.payload_chunk.offset = 0
        pt.payload_chunk.body = body
        await self.send_encrypted(first)

        last = ow.OfflineFrame()
        last.CopyFrom(first)
        lpt = last.v1.payload_transfer
        lpt.payload_chunk.offset = len(body)
        lpt.payload_chunk.flags = ow.PayloadTransferFrame.PayloadChunk.LAST_CHUNK
        lpt.payload_chunk.ClearField("body")
        await self.send_encrypted(last)

    async def recv_sharing_frame(self) -> wf.Frame:
        buffers: dict[int, bytearray] = {}
        while True:
            frame = await self.recv_encrypted()
            if frame.v1.type != ow.V1Frame.PAYLOAD_TRANSFER:
                continue
            pt = frame.v1.payload_transfer
            if pt.payload_header.type != ow.PayloadTransferFrame.PayloadHeader.BYTES:
                continue
            buf = buffers.setdefault(pt.payload_header.id, bytearray())
            buf.extend(pt.payload_chunk.body)
            if pt.payload_chunk.flags & ow.PayloadTransferFrame.PayloadChunk.LAST_CHUNK:
                sharing = wf.Frame()
                sharing.ParseFromString(bytes(buffers.pop(pt.payload_header.id)))
                return sharing

    async def send_introduction(self, files: list[dict] | None = None,
                                texts: list[dict] | None = None) -> wf.Frame:
        intro = wf.Frame()
        intro.version = wf.Frame.V1
        intro.v1.type = wf.V1Frame.INTRODUCTION
        for fm in files or []:
            entry = intro.v1.introduction.file_metadata.add()
            entry.name = fm["name"]
            entry.payload_id = fm["payload_id"]
            entry.size = fm["size"]
            entry.mime_type = fm.get("mime_type", "application/octet-stream")
        for tm in texts or []:
            entry = intro.v1.introduction.text_metadata.add()
            entry.text_title = tm["title"]
            entry.payload_id = tm["payload_id"]
            entry.size = tm["size"]
            entry.type = wf.TextMetadata.TEXT
        await self.send_sharing_frame(intro)

        resp = await self.recv_sharing_frame()
        assert resp.v1.type == wf.V1Frame.RESPONSE, resp
        assert (resp.v1.connection_response.status ==
                wf.ConnectionResponseFrame.ACCEPT), "receiver declined"
        return resp

    async def send_file_chunk(self, payload_id: int, size: int, offset: int,
                              body: bytes, name: str, last: bool) -> None:
        frame = ow.OfflineFrame()
        frame.version = ow.OfflineFrame.V1
        frame.v1.type = ow.V1Frame.PAYLOAD_TRANSFER
        pt = frame.v1.payload_transfer
        pt.packet_type = ow.PayloadTransferFrame.DATA
        pt.payload_header.id = payload_id
        pt.payload_header.type = ow.PayloadTransferFrame.PayloadHeader.FILE
        pt.payload_header.total_size = size
        pt.payload_header.file_name = name
        pt.payload_chunk.offset = offset
        pt.payload_chunk.flags = (
            ow.PayloadTransferFrame.PayloadChunk.LAST_CHUNK if last else 0)
        if body:
            pt.payload_chunk.body = body
        await self.send_encrypted(frame)

    def file_chunks(self, data: bytes) -> list[tuple[int, bytes, bool]]:
        """Split `data` into (offset, body, is_last) tuples the same way
        OutboundConnection._send_file does: CHUNK_SIZE pieces, then a
        trailing empty LAST_CHUNK marker."""
        chunks = []
        offset = 0
        while offset < len(data):
            piece = data[offset:offset + CHUNK_SIZE]
            chunks.append((offset, piece, False))
            offset += len(piece)
        chunks.append((offset, b"", True))
        return chunks


# ---------------------------------------------------------------- harness

async def start_receiver(download_dir: Path, events: Events
                         ) -> tuple[asyncio.AbstractServer, int]:
    async def handle(reader, writer):
        conn = InboundConnection(reader, writer, events,
                                 "test-receiver", download_dir)
        await conn.run()

    server = await asyncio.start_server(handle, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def connect_sender(port: int) -> FakeSender:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    sender = FakeSender(reader, writer)
    await sender.handshake()
    return sender


async def finish(sender: FakeSender, server: asyncio.AbstractServer) -> None:
    writer = sender.writer
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionError, OSError):
        pass
    server.close()
    await server.wait_closed()


def progress_update_frame_bytes(progress: float = 0.5) -> bytes:
    """A serialized sharing.nearby Frame that looks like a real
    ProgressUpdateFrame control message -- the kind of thing that
    previously got misdelivered as text."""
    frame = wf.Frame()
    frame.version = wf.Frame.V1
    frame.v1.type = wf.V1Frame.PROGRESS_UPDATE
    frame.v1.progress_update.progress = progress
    frame.v1.progress_update.start_transfer = False
    return frame.SerializeToString()


# ---------------------------------------------------------------- tests

async def test_extra_bytes_payload_not_delivered_as_text(tmp: Path) -> None:
    """1. File transfer with an extra undeclared BYTES payload (a
    ProgressUpdateFrame-style control frame) injected mid-transfer.
    on_text must never fire and the file must still arrive intact."""
    downloads = tmp / "t1"
    data = os.urandom(600 * 1024)

    got_text: list[str] = []
    completed: list[list[Path]] = []
    events = Events(
        on_transfer_request=lambda req: _accept(),
        on_text=lambda dev, text: got_text.append(text),
        on_complete=lambda dev, paths: completed.append(paths),
    )
    server, port = await start_receiver(downloads, events)
    sender = await connect_sender(port)

    payload_id = rand_payload_id()
    await sender.send_introduction(files=[
        {"name": "photo.bin", "payload_id": payload_id, "size": len(data)}])

    chunks = sender.file_chunks(data)
    # First real chunk, then an undeclared BYTES control frame injected
    # mid-transfer, then the rest of the file.
    off, body, last = chunks[0]
    await sender.send_file_chunk(payload_id, len(data), off, body, "photo.bin", last)
    await sender.send_bytes_payload(rand_payload_id(), progress_update_frame_bytes())
    for off, body, last in chunks[1:]:
        await sender.send_file_chunk(payload_id, len(data), off, body, "photo.bin", last)

    await asyncio.sleep(0.3)
    await finish(sender, server)

    assert not got_text, f"on_text must never fire, got {got_text}"
    assert completed, "on_complete never fired"
    dest = completed[0][0]
    assert dest.read_bytes() == data, "file corrupted"
    print("test 1 passed: undeclared BYTES control frame ignored, file intact")


async def test_declared_text_share(tmp: Path) -> None:
    """2. A declared text share (TextMetadata in INTRODUCTION, matching
    payload_id BYTES payload). on_text must fire with the exact string and
    no file events should fire."""
    downloads = tmp / "t2"
    text = "hello from the fake sender — regression test"
    text_bytes = text.encode("utf-8")

    got_text: list[tuple[str, str]] = []
    progress_events: list[int] = []
    completed: list[list[Path]] = []
    events = Events(
        on_transfer_request=lambda req: _accept(),
        on_text=lambda dev, t: got_text.append((dev, t)),
        on_progress=lambda dev, done, total: progress_events.append(done),
        on_complete=lambda dev, paths: completed.append(paths),
    )
    server, port = await start_receiver(downloads, events)
    sender = await connect_sender(port)

    payload_id = rand_payload_id()
    await sender.send_introduction(texts=[
        {"title": "note", "payload_id": payload_id, "size": len(text_bytes)}])
    await sender.send_bytes_payload(payload_id, text_bytes)

    await asyncio.sleep(0.3)
    await finish(sender, server)

    assert len(got_text) == 1, f"on_text should fire exactly once, got {got_text}"
    assert got_text[0][1] == text, f"text mismatch: {got_text[0][1]!r}"
    assert not progress_events, f"no file progress expected, got {progress_events}"
    assert not any(downloads.glob("**/*")), "no file should have been written"
    print("test 2 passed: declared text share delivered exactly once")


async def test_cancel_mid_transfer(tmp: Path) -> None:
    """3. Sender CANCEL mid-transfer (a wf.Frame V1 CANCEL sent as a BYTES
    payload after the first file chunk). The partial file must be deleted
    and on_complete must not fire."""
    downloads = tmp / "t3"
    data = os.urandom(600 * 1024)

    completed: list[list[Path]] = []
    events = Events(
        on_transfer_request=lambda req: _accept(),
        on_complete=lambda dev, paths: completed.append(paths),
    )
    server, port = await start_receiver(downloads, events)
    sender = await connect_sender(port)

    payload_id = rand_payload_id()
    await sender.send_introduction(files=[
        {"name": "movie.bin", "payload_id": payload_id, "size": len(data)}])

    chunks = sender.file_chunks(data)
    off, body, last = chunks[0]
    await sender.send_file_chunk(payload_id, len(data), off, body, "movie.bin", last)

    cancel = wf.Frame()
    cancel.version = wf.Frame.V1
    cancel.v1.type = wf.V1Frame.CANCEL
    await sender.send_bytes_payload(rand_payload_id(), cancel.SerializeToString())

    await asyncio.sleep(0.3)
    await finish(sender, server)

    assert not completed, "on_complete must not fire after a cancel"
    leftover = list(downloads.glob("**/*"))
    leftover_files = [p for p in leftover if p.is_file()]
    assert not leftover_files, f"partial file(s) not cleaned up: {leftover_files}"
    print("test 3 passed: cancel mid-transfer deletes partial file, no on_complete")


async def test_interleaved_files(tmp: Path) -> None:
    """4. Two files interleaved chunk-wise (alternating chunks between two
    payload ids). Both must arrive byte-identical."""
    downloads = tmp / "t4"
    data_a = os.urandom(620 * 1024)
    data_b = os.urandom(680 * 1024)

    completed: list[list[Path]] = []
    events = Events(
        on_transfer_request=lambda req: _accept(),
        on_complete=lambda dev, paths: completed.append(paths),
    )
    server, port = await start_receiver(downloads, events)
    sender = await connect_sender(port)

    id_a, id_b = rand_payload_id(), rand_payload_id()
    await sender.send_introduction(files=[
        {"name": "a.bin", "payload_id": id_a, "size": len(data_a)},
        {"name": "b.bin", "payload_id": id_b, "size": len(data_b)},
    ])

    chunks_a = sender.file_chunks(data_a)
    chunks_b = sender.file_chunks(data_b)
    assert len(chunks_a) > 2 and len(chunks_b) > 2, \
        "test needs multiple 512KB chunks per file to actually interleave"

    max_len = max(len(chunks_a), len(chunks_b))
    for i in range(max_len):
        if i < len(chunks_a):
            off, body, last = chunks_a[i]
            await sender.send_file_chunk(id_a, len(data_a), off, body, "a.bin", last)
        if i < len(chunks_b):
            off, body, last = chunks_b[i]
            await sender.send_file_chunk(id_b, len(data_b), off, body, "b.bin", last)

    await asyncio.sleep(0.3)
    await finish(sender, server)

    assert completed, "on_complete never fired"
    by_name = {p.name: p for p in completed[0]}
    assert set(by_name) == {"a.bin", "b.bin"}, by_name
    assert by_name["a.bin"].read_bytes() == data_a, "a.bin corrupted"
    assert by_name["b.bin"].read_bytes() == data_b, "b.bin corrupted"
    print("test 4 passed: interleaved chunks from two files both intact")


async def test_duplicate_chunk(tmp: Path) -> None:
    """5. Duplicate/retransmitted chunk (same offset sent twice). Final
    file must still be byte-identical."""
    downloads = tmp / "t5"
    data = os.urandom(600 * 1024)

    completed: list[list[Path]] = []
    events = Events(
        on_transfer_request=lambda req: _accept(),
        on_complete=lambda dev, paths: completed.append(paths),
    )
    server, port = await start_receiver(downloads, events)
    sender = await connect_sender(port)

    payload_id = rand_payload_id()
    await sender.send_introduction(files=[
        {"name": "dup.bin", "payload_id": payload_id, "size": len(data)}])

    chunks = sender.file_chunks(data)
    assert len(chunks) > 2, "test needs at least 2 data chunks plus terminator"

    # Send the first chunk, then resend it again (retransmit) before moving
    # on to the rest -- the receiver must seek back and overwrite rather
    # than duplicate the bytes in the output file.
    off0, body0, last0 = chunks[0]
    await sender.send_file_chunk(payload_id, len(data), off0, body0, "dup.bin", last0)
    await sender.send_file_chunk(payload_id, len(data), off0, body0, "dup.bin", last0)
    for off, body, last in chunks[1:]:
        await sender.send_file_chunk(payload_id, len(data), off, body, "dup.bin", last)

    await asyncio.sleep(0.3)
    await finish(sender, server)

    assert completed, "on_complete never fired"
    dest = completed[0][0]
    assert dest.stat().st_size == len(data), \
        f"size mismatch: {dest.stat().st_size} != {len(data)}"
    assert dest.read_bytes() == data, "duplicate chunk corrupted the file"
    print("test 5 passed: retransmitted chunk does not corrupt output")


async def _accept(*_a, **_kw) -> bool:
    return True


# ---------------------------------------------------------------- main

async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="qs-payload-routing-"))
    try:
        await test_extra_bytes_payload_not_delivered_as_text(tmp)
        await test_declared_text_share(tmp)
        await test_cancel_mid_transfer(tmp)
        await test_interleaved_files(tmp)
        await test_duplicate_chunk(tmp)
    finally:
        pass

    print("\nPAYLOAD ROUTING REGRESSION TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
