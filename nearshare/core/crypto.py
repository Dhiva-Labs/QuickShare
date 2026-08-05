"""Ukey2 handshake and SecureMessage session crypto for Nearby Share.

This is the interop-critical layer: it implements Google's UKEY2 key
exchange (P256_SHA512 cipher) and the securegcm device-to-device
SecureMessage framing exactly as Android's Quick Share stack does.
References: google/ukey2 spec, securegcm D2DConnectionContextV1,
NearDrop and rnearshare implementations.

Wire flow (over 4-byte big-endian length-prefixed frames):
    sender (client)                      receiver (server)
    Ukey2Message(CLIENT_INIT)   ------>  (M1 = raw bytes)
                                <------  Ukey2Message(SERVER_INIT)  (M2)
    Ukey2Message(CLIENT_FINISH) ------>  (commitment check: SHA512(M3))

Key schedule:
    dhs         = SHA256(ECDH-P256 shared x-coordinate)
    auth_string = HKDF(ikm=dhs, salt="UKEY2 v1 auth", info=M1|M2, 32)
    next_secret = HKDF(ikm=dhs, salt="UKEY2 v1 next", info=M1|M2, 32)
    d2d_salt    = SHA256("D2D")
    client_key  = HKDF(ikm=next_secret, salt=d2d_salt, info="client", 32)
    server_key  = HKDF(ikm=next_secret, salt=d2d_salt, info="server", 32)
    sm_salt     = SHA256("SecureMessage")
    enc_key(k)  = HKDF(ikm=k, salt=sm_salt, info="ENC:2", 32)
    sig_key(k)  = HKDF(ikm=k, salt=sm_salt, info="SIG:1", 32)

Each direction then sends SecureMessage{HeaderAndBody, HMAC-SHA256}
whose body is an AES-256-CBC encrypted DeviceToDeviceMessage wrapping
one serialized OfflineFrame, with a per-direction sequence number
starting at 1.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..proto import (device_to_device_messages_pb2, securegcm_pb2,
                     securemessage_pb2, ukey_pb2)

NEXT_PROTOCOL = "AES_256_CBC-HMAC_SHA256"
UKEY_CLIENT_VERSION = 1

_D2D_SALT = hashlib.sha256(b"D2D").digest()
_SM_SALT = hashlib.sha256(b"SecureMessage").digest()


class HandshakeError(Exception):
    """Raised when the peer violates the UKEY2 protocol."""


def _hkdf(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt,
                info=info).derive(ikm)


def _to_twos_complement(n: int) -> bytes:
    """Big-endian two's complement encoding of a positive integer,
    matching Java BigInteger.toByteArray() as Android expects."""
    raw = n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return raw


def _encode_public_key(pub: ec.EllipticCurvePublicKey) -> bytes:
    nums = pub.public_numbers()
    gpk = securemessage_pb2.GenericPublicKey()
    gpk.type = securemessage_pb2.EC_P256
    gpk.ec_p256_public_key.x = _to_twos_complement(nums.x)
    gpk.ec_p256_public_key.y = _to_twos_complement(nums.y)
    return gpk.SerializeToString()


def _decode_public_key(data: bytes) -> ec.EllipticCurvePublicKey:
    gpk = securemessage_pb2.GenericPublicKey()
    gpk.ParseFromString(data)
    if gpk.type != securemessage_pb2.EC_P256:
        raise HandshakeError(f"unsupported public key type {gpk.type}")
    x = int.from_bytes(gpk.ec_p256_public_key.x, "big", signed=True)
    y = int.from_bytes(gpk.ec_p256_public_key.y, "big", signed=True)
    return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()


def pin_code(auth_string: bytes) -> str:
    """4-digit verification PIN both screens show, derived from the UKEY2
    auth string with Android's exact polynomial hash."""
    acc = 0
    mult = 1
    for b in auth_string:
        signed = b - 256 if b >= 128 else b
        acc = (acc + signed * mult) % 9973
        mult = (mult * 31) % 9973
    return f"{abs(acc) % 10000:04d}"


@dataclass
class SessionKeys:
    auth_string: bytes
    encrypt_key: bytes   # ENC key for frames we send
    send_hmac_key: bytes  # SIG key for frames we send
    decrypt_key: bytes   # ENC key for frames we receive
    recv_hmac_key: bytes  # SIG key for frames we receive

    @property
    def pin(self) -> str:
        return pin_code(self.auth_string)


def _derive_session_keys(dhs: bytes, m1: bytes, m2: bytes,
                         is_server: bool) -> SessionKeys:
    auth_string = _hkdf(dhs, b"UKEY2 v1 auth", m1 + m2)
    next_secret = _hkdf(dhs, b"UKEY2 v1 next", m1 + m2)
    client_key = _hkdf(next_secret, _D2D_SALT, b"client")
    server_key = _hkdf(next_secret, _D2D_SALT, b"server")
    own, peer = (server_key, client_key) if is_server else (client_key, server_key)
    return SessionKeys(
        auth_string=auth_string,
        encrypt_key=_hkdf(own, _SM_SALT, b"ENC:2"),
        send_hmac_key=_hkdf(own, _SM_SALT, b"SIG:1"),
        decrypt_key=_hkdf(peer, _SM_SALT, b"ENC:2"),
        recv_hmac_key=_hkdf(peer, _SM_SALT, b"SIG:1"),
    )


class Ukey2Server:
    """Receiver side of the handshake (the Android sender is the client)."""

    def __init__(self) -> None:
        self._private = ec.generate_private_key(ec.SECP256R1())
        self._m1: bytes | None = None
        self._m2: bytes | None = None
        self._commitment: bytes | None = None

    def handle_client_init(self, m1: bytes) -> bytes:
        """Consume raw CLIENT_INIT message bytes, return SERVER_INIT bytes."""
        msg = ukey_pb2.Ukey2Message()
        msg.ParseFromString(m1)
        if msg.message_type != ukey_pb2.Ukey2Message.CLIENT_INIT:
            raise HandshakeError("expected CLIENT_INIT")
        init = ukey_pb2.Ukey2ClientInit()
        init.ParseFromString(msg.message_data)
        for commitment in init.cipher_commitments:
            if commitment.handshake_cipher == ukey_pb2.P256_SHA512:
                self._commitment = commitment.commitment
                break
        else:
            raise HandshakeError("no P256_SHA512 commitment offered")

        server_init = ukey_pb2.Ukey2ServerInit()
        server_init.version = init.version
        server_init.random = os.urandom(32)
        server_init.handshake_cipher = ukey_pb2.P256_SHA512
        server_init.public_key = _encode_public_key(self._private.public_key())
        out = ukey_pb2.Ukey2Message()
        out.message_type = ukey_pb2.Ukey2Message.SERVER_INIT
        out.message_data = server_init.SerializeToString()
        self._m1 = m1
        self._m2 = out.SerializeToString()
        return self._m2

    def handle_client_finish(self, m3: bytes) -> SessionKeys:
        if self._commitment is None or self._m1 is None or self._m2 is None:
            raise HandshakeError("client finish before client init")
        if not hmac_mod.compare_digest(hashlib.sha512(m3).digest(),
                                       self._commitment):
            raise HandshakeError("CLIENT_FINISH does not match commitment")
        msg = ukey_pb2.Ukey2Message()
        msg.ParseFromString(m3)
        if msg.message_type != ukey_pb2.Ukey2Message.CLIENT_FINISH:
            raise HandshakeError("expected CLIENT_FINISH")
        finish = ukey_pb2.Ukey2ClientFinished()
        finish.ParseFromString(msg.message_data)
        peer = _decode_public_key(finish.public_key)
        dhs = hashlib.sha256(self._private.exchange(ec.ECDH(), peer)).digest()
        return _derive_session_keys(dhs, self._m1, self._m2, is_server=True)


class Ukey2Client:
    """Sender side of the handshake (we initiate towards the receiver)."""

    def __init__(self) -> None:
        self._private = ec.generate_private_key(ec.SECP256R1())
        # CLIENT_FINISH is fixed up front; CLIENT_INIT commits to its hash.
        finish = ukey_pb2.Ukey2ClientFinished()
        finish.public_key = _encode_public_key(self._private.public_key())
        msg = ukey_pb2.Ukey2Message()
        msg.message_type = ukey_pb2.Ukey2Message.CLIENT_FINISH
        msg.message_data = finish.SerializeToString()
        self._m3 = msg.SerializeToString()
        self._m1: bytes | None = None

    def client_init(self) -> bytes:
        init = ukey_pb2.Ukey2ClientInit()
        init.version = UKEY_CLIENT_VERSION
        init.random = os.urandom(32)
        commitment = init.cipher_commitments.add()
        commitment.handshake_cipher = ukey_pb2.P256_SHA512
        commitment.commitment = hashlib.sha512(self._m3).digest()
        init.next_protocol = NEXT_PROTOCOL
        msg = ukey_pb2.Ukey2Message()
        msg.message_type = ukey_pb2.Ukey2Message.CLIENT_INIT
        msg.message_data = init.SerializeToString()
        self._m1 = msg.SerializeToString()
        return self._m1

    def handle_server_init(self, m2: bytes) -> tuple[bytes, SessionKeys]:
        """Consume SERVER_INIT, return (CLIENT_FINISH bytes, session keys)."""
        if self._m1 is None:
            raise HandshakeError("server init before client init")
        msg = ukey_pb2.Ukey2Message()
        msg.ParseFromString(m2)
        if msg.message_type != ukey_pb2.Ukey2Message.SERVER_INIT:
            raise HandshakeError("expected SERVER_INIT")
        server_init = ukey_pb2.Ukey2ServerInit()
        server_init.ParseFromString(msg.message_data)
        if server_init.handshake_cipher != ukey_pb2.P256_SHA512:
            raise HandshakeError("server selected unsupported cipher")
        peer = _decode_public_key(server_init.public_key)
        dhs = hashlib.sha256(self._private.exchange(ec.ECDH(), peer)).digest()
        keys = _derive_session_keys(dhs, self._m1, m2, is_server=False)
        return self._m3, keys


class D2DCipher:
    """Encrypts/decrypts OfflineFrame payloads as securegcm SecureMessages."""

    def __init__(self, keys: SessionKeys) -> None:
        self._keys = keys
        self._send_seq = 0
        self._recv_seq = 0

    def encrypt(self, plaintext_frame: bytes) -> bytes:
        self._send_seq += 1
        d2d = device_to_device_messages_pb2.DeviceToDeviceMessage()
        d2d.message = plaintext_frame
        d2d.sequence_number = self._send_seq

        iv = os.urandom(16)
        padder = padding.PKCS7(128).padder()
        padded = padder.update(d2d.SerializeToString()) + padder.finalize()
        enc = Cipher(algorithms.AES(self._keys.encrypt_key),
                     modes.CBC(iv)).encryptor()
        body = enc.update(padded) + enc.finalize()

        metadata = securegcm_pb2.GcmMetadata()
        metadata.type = securegcm_pb2.DEVICE_TO_DEVICE_MESSAGE
        metadata.version = 1

        hb = securemessage_pb2.HeaderAndBody()
        hb.header.signature_scheme = securemessage_pb2.HMAC_SHA256
        hb.header.encryption_scheme = securemessage_pb2.AES_256_CBC
        hb.header.iv = iv
        hb.header.public_metadata = metadata.SerializeToString()
        hb.body = body
        hb_bytes = hb.SerializeToString()

        sm = securemessage_pb2.SecureMessage()
        sm.header_and_body = hb_bytes
        sm.signature = hmac_mod.new(self._keys.send_hmac_key, hb_bytes,
                                    hashlib.sha256).digest()
        return sm.SerializeToString()

    def decrypt(self, data: bytes) -> bytes:
        sm = securemessage_pb2.SecureMessage()
        sm.ParseFromString(data)
        expected = hmac_mod.new(self._keys.recv_hmac_key, sm.header_and_body,
                                hashlib.sha256).digest()
        if not hmac_mod.compare_digest(expected, sm.signature):
            raise HandshakeError("bad frame HMAC")
        hb = securemessage_pb2.HeaderAndBody()
        hb.ParseFromString(sm.header_and_body)
        dec = Cipher(algorithms.AES(self._keys.decrypt_key),
                     modes.CBC(hb.header.iv)).decryptor()
        padded = dec.update(hb.body) + dec.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        plain = unpadder.update(padded) + unpadder.finalize()
        d2d = device_to_device_messages_pb2.DeviceToDeviceMessage()
        d2d.ParseFromString(plain)
        self._recv_seq += 1
        if d2d.sequence_number != self._recv_seq:
            raise HandshakeError(
                f"sequence mismatch: got {d2d.sequence_number}, "
                f"expected {self._recv_seq}")
        return d2d.message

    @property
    def pin(self) -> str:
        return self._keys.pin
