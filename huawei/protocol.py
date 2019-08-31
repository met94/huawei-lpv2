import binascii
import hashlib
import hmac
import math
import secrets

from typing import List

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

HUAWEI_LPV2_MAGIC = b"\x5A"

PROTOCOL_VERSION = 2
AUTH_VERSION = 1
NONCE_LENGTH = 16

NETWORK_BYTEORDER = "big"

DIGEST_PREFIX = "70 FB 6C 24 03 5F DB 55 2F 38 89 8A EE DE 3F 69"

MESSAGE_RESPONSE = "0110"
MESSAGE_CHALLENGE = "0100"

SECRET_KEY_1 = "6F 75 6A 79 6D 77 71 34 63 6C 76 39 33 37 38 79"
SECRET_KEY_2 = "62 31 30 6A 67 66 64 39 79 37 76 73 75 64 61 39"


def encode_int(value: int, length: int = 2) -> bytes:
    return value.to_bytes(length=length, byteorder=NETWORK_BYTEORDER)


def decode_int(data: bytes, signed: bool = False) -> int:
    return int.from_bytes(data, byteorder=NETWORK_BYTEORDER, signed=signed)


def hexlify(data: bytes) -> str:
    result = binascii.hexlify(data).upper()
    return " ".join(chr(odd) + chr(even) for odd, even in zip(result[::2], result[1::2]))


class VarInt:
    def __init__(self, value: int):
        if value < 0:
            raise ValueError("variable-length integer must be non-negative")
        self._value = value

    def __repr__(self):
        return f"VarInt({self._value})"

    def __eq__(self, other: "VarInt"):
        return self._value == other._value

    def __int__(self):
        return self._value

    def __len__(self):
        return math.ceil(math.log(self._value + 1, 2 ** 7)) if self._value > 2 else 1

    def __bytes__(self):
        data = []
        value = self._value
        while value >= 0:
            current_byte = value & 0b01111111
            data.append(current_byte | 0b10000000)
            if current_byte == value:
                data[0] &= 0b01111111
                return bytes(reversed(data))
            value >>= 7

    @staticmethod
    def from_bytes(data: bytes):
        value = 0
        for current_byte in data:
            value += current_byte & 0b01111111
            if not current_byte & 0b10000000:
                return VarInt(value)
            value <<= 7


class TLV:
    def __init__(self, tag: int, value: bytes = b""):
        self.tag = tag
        self.value = value

    def __repr__(self):
        return f"TLV(tag={self.tag}, value=bytes.fromhex('{hexlify(self.value)}'))"

    def __eq__(self, other: "TLV"):
        return (self.tag, self.value) == (other.tag, other.value)

    def __len__(self):
        return len(bytes(self))

    def __bytes__(self):
        return bytes([self.tag]) + bytes(VarInt(len(self.value))) + self.value

    @staticmethod
    def from_bytes(data: bytes):
        tag, body = data[0], data[1:]

        value_length = VarInt.from_bytes(body)
        value_begin, value_end = len(value_length), len(value_length) + int(value_length)

        return TLV(tag=tag, value=body[value_begin:value_end])


class Command:
    def __init__(self, tlvs: List[TLV] = None):
        self.tlvs = tlvs if tlvs is not None else []

    def __repr__(self):
        return f"Command(tlvs={self.tlvs})"

    def __eq__(self, other: "Command"):
        return self.tlvs == other.tlvs

    def __contains__(self, tag: int):
        return any(item.tag == tag for item in self.tlvs)

    def __getitem__(self, tag: int):
        return next(filter(lambda item: item.tag == tag, self.tlvs))

    def __bytes__(self):
        return b"".join(map(bytes, self.tlvs))

    @staticmethod
    def from_bytes(data: bytes):
        tlvs = []
        while len(data):
            tlv = TLV.from_bytes(data)
            tlvs.append(tlv)
            data = data[len(tlv):]
        return Command(tlvs=tlvs)


class Packet:
    def __init__(self, service_id: int, command_id: int, command: Command):
        self.service_id = service_id
        self.command_id = command_id
        self.command = command

    def __repr__(self):
        return f"Packet(service_id={self.service_id}, command_id={self.command_id}, command={self.command})"

    def __eq__(self, other: "Packet"):
        return (self.service_id, self.command_id, self.command) == (other.service_id, other.command_id, other.command)

    def __bytes__(self) -> bytes:
        payload = bytes([self.service_id, self.command_id]) + bytes(self.command)

        if len(payload) > (2 ** 8) * 2:
            raise ValueError(f"payload too large: {len(payload)}")

        data = HUAWEI_LPV2_MAGIC + encode_int(len(payload) + 1) + b"\0" + payload

        return data + encode_int(binascii.crc_hqx(data, 0))

    @staticmethod
    def from_bytes(data: bytes) -> "Packet":

        if len(data) < 1 + 2 + 1 + 2:
            raise ValueError("packet too short")

        magic, length, padding, payload, checksum = data[0], data[1:2], data[3], data[4:-2], data[-2:]

        if magic != ord(HUAWEI_LPV2_MAGIC):
            raise ValueError(f"magic mismatch: {magic} != {HUAWEI_LPV2_MAGIC}")

        actual_checksum = encode_int(binascii.crc_hqx(data[:-2], 0))

        if actual_checksum != checksum:
            raise ValueError(f"checksum mismatch: {actual_checksum} != {checksum}")

        return Packet(service_id=payload[0], command_id=payload[1], command=Command.from_bytes(payload[2:]))


def compute_digest(message: str, server_nonce: bytes, client_nonce: bytes):
    nonce = server_nonce + client_nonce

    def digest(key: bytes, msg: bytes):
        return hmac.new(key, msg=msg, digestmod=hashlib.sha256).digest()

    return digest(digest(bytes.fromhex(DIGEST_PREFIX + message), nonce), nonce)


def digest_challenge(server_nonce: bytes, client_nonce: bytes):
    return compute_digest(MESSAGE_CHALLENGE, server_nonce, client_nonce)


def digest_response(server_nonce: bytes, client_nonce: bytes):
    return compute_digest(MESSAGE_RESPONSE, server_nonce, client_nonce)


def generate_nonce() -> bytes:
    return secrets.token_bytes(NONCE_LENGTH)


def encrypt(data: bytes, secret: bytes, iv: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded_data = padder.update(data) + padder.finalize()

    backend = default_backend()
    cipher = Cipher(algorithms.AES(secret), modes.CBC(iv), backend=backend)
    encryptor = cipher.encryptor()

    return encryptor.update(padded_data) + encryptor.finalize()


def decrypt(data: bytes, secret: bytes, iv: bytes) -> bytes:
    backend = default_backend()
    cipher = Cipher(algorithms.AES(secret), modes.CBC(iv), backend=backend)
    decryptor = cipher.decryptor()
    padded_data = decryptor.update(data) + decryptor.finalize()

    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded_data) + unpadder.finalize()


def create_secret_key(mac_address: str) -> bytes:
    mac_address_key = (mac_address.replace(":", "") + "0000").encode()

    mixed_secret_key = [
        ((key1_byte << 4) ^ key2_byte) % 0xFF
        for key1_byte, key2_byte in zip(bytes.fromhex(SECRET_KEY_1), bytes.fromhex(SECRET_KEY_2))
    ]

    mixed_secret_key_hash = hashlib.sha256(bytes(mixed_secret_key)).digest()

    final_mixed_key = [
        ((mixed_key_hash_byte >> 6) ^ mac_address_byte) % 0xFF
        for mixed_key_hash_byte, mac_address_byte in zip(mixed_secret_key_hash, mac_address_key)
    ]

    return hashlib.sha256(bytes(final_mixed_key)).digest()


def create_bonding_data(mac_address: str, iv: bytes) -> bytes:
    return encrypt(generate_nonce(), create_secret_key(mac_address), iv)
