"""Chain-specific address validation.

Kept dependency-light on purpose: base58 is implemented inline, and EIP-55
checksum validation degrades gracefully when pycryptodome is unavailable.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# keccak-256 (optional, used only for EIP-55 checksum verification)
# --------------------------------------------------------------------------
try:
    from Crypto.Hash import keccak as _keccak

    def keccak256(data: bytes) -> bytes:
        h = _keccak.new(digest_bits=256)
        h.update(data)
        return h.digest()

    HAVE_KECCAK = True
except Exception:  # pragma: no cover - depends on the install
    HAVE_KECCAK = False

    def keccak256(data: bytes) -> bytes:  # type: ignore[misc]
        raise RuntimeError("keccak256 unavailable: pip install pycryptodome")


# --------------------------------------------------------------------------
# base58 (Solana / Bitcoin alphabet)
# --------------------------------------------------------------------------
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}


def b58decode(value: str) -> bytes:
    """Decode a base58 string. Raises ValueError on an invalid character."""
    num = 0
    for ch in value:
        idx = _B58_INDEX.get(ch)
        if idx is None:
            raise ValueError(f"invalid base58 character: {ch!r}")
        num = num * 58 + idx

    leading_zeros = len(value) - len(value.lstrip("1"))
    body = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    return b"\x00" * leading_zeros + body


# --------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------
EVM_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
# Solana mints are 32 raw bytes -> 32..44 base58 chars. Bounded to avoid
# matching long base58-ish blobs (JWTs, hashes, asset fingerprints).
SOLANA_RE = re.compile(rf"\b[{_B58_ALPHABET}]{{32,44}}\b")

CHAIN_EVM = "evm"
CHAIN_SOLANA = "solana"


# --------------------------------------------------------------------------
# Known non-contract addresses that show up constantly in swap links / UIs.
# Anything here is discarded before it can become an alert.
# --------------------------------------------------------------------------
DENYLIST: set[str] = {
    # EVM
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
    "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
    "0x55d398326f99059ff775485246999027b3197955",  # BSC-USD
    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB
    # Solana
    "so11111111111111111111111111111111111111112",  # wSOL
    "epjfwdd5aufqssqem2qn1xzybapc8g4weggkzwytdt1v",  # USDC
    "es9vmfrzacermjfrf4h2fyd4kconky11mcce8benwnyb",  # USDT
    "tokenkegqfezyinwajbnbgkpfxcwubvf9ss623vq5da",  # SPL Token program
    "11111111111111111111111111111111",  # System program
    "computebudget111111111111111111111111111111",
    "atokengpvbdgvxr1b2hvzbsiqw8lcu5ibegrnhxy2zs",  # Associated token program
}


def normalize(address: str, chain: str) -> str:
    """Canonical dedup key form for an address."""
    return address.lower() if chain == CHAIN_EVM else address


def is_denied(address: str) -> bool:
    return address.lower() in DENYLIST


def validate_evm(address: str) -> tuple[bool, str]:
    """Validate an EVM address.

    Returns (ok, reason). A mixed-case address carries an EIP-55 checksum; if
    that checksum does not verify the address is corrupt or spoofed and we
    reject it. A uniformly-cased address carries no checksum information and is
    accepted on shape alone.
    """
    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", address):
        return False, "bad_shape"

    body = address[2:]
    if body == body.lower() or body == body.upper():
        return True, "no_checksum"

    if not HAVE_KECCAK:
        log.debug("skipping EIP-55 check for %s (pycryptodome not installed)", address)
        return True, "checksum_unverified"

    digest = keccak256(body.lower().encode()).hex()
    expected = "".join(
        c.upper() if int(digest[i], 16) >= 8 else c for i, c in enumerate(body.lower())
    )
    if expected == body:
        return True, "checksum_ok"
    return False, "checksum_failed"


def validate_solana(address: str) -> tuple[bool, str]:
    """Validate a Solana address: base58 that decodes to exactly 32 bytes."""
    if not (32 <= len(address) <= 44):
        return False, "bad_length"
    try:
        raw = b58decode(address)
    except ValueError:
        return False, "bad_base58"
    if len(raw) != 32:
        return False, f"decoded_{len(raw)}_bytes"
    return True, "ok"


def validate(address: str, chain: str) -> tuple[bool, str]:
    if is_denied(address):
        return False, "denylisted"
    if chain == CHAIN_EVM:
        return validate_evm(address)
    if chain == CHAIN_SOLANA:
        return validate_solana(address)
    return False, f"unknown_chain_{chain}"


def explorer_links(address: str, chain: str) -> list[tuple[str, str]]:
    """(label, url) pairs for quick manual verification from the alert."""
    if chain == CHAIN_EVM:
        return [
            ("Dexscreener", f"https://dexscreener.com/search?q={address}"),
            ("Etherscan", f"https://etherscan.io/address/{address}"),
            ("BscScan", f"https://bscscan.com/address/{address}"),
        ]
    if chain == CHAIN_SOLANA:
        return [
            ("Dexscreener", f"https://dexscreener.com/solana/{address}"),
            ("Solscan", f"https://solscan.io/token/{address}"),
            ("Birdeye", f"https://birdeye.so/token/{address}?chain=solana"),
        ]
    return []
