import pytest

from sentinel.chains import (
    CHAIN_EVM,
    CHAIN_SOLANA,
    b58decode,
    is_denied,
    normalize,
    validate,
    validate_evm,
    validate_solana,
)


class TestEvm:
    def test_valid_checksummed(self):
        ok, reason = validate_evm("0x5B6Ef408c4eBb166788C0cA4cB644f12AC757777")
        assert ok and reason == "checksum_ok"

    def test_valid_checksummed_secretarea(self):
        ok, _ = validate_evm("0x008Df4b3E857D06c4603Aeb11F267ccD32ce2005")
        assert ok

    def test_all_lowercase_accepted_without_checksum(self):
        ok, reason = validate_evm("0x5b6ef408c4ebb166788c0ca4cb644f12ac757777")
        assert ok and reason == "no_checksum"

    def test_all_uppercase_accepted(self):
        ok, reason = validate_evm("0x5B6EF408C4EBB166788C0CA4CB644F12AC757777")
        assert ok and reason == "no_checksum"

    def test_corrupted_checksum_rejected(self):
        # Correct form is 0x5B6Ef408...; flipping the case of the first bytes
        # keeps it mixed-case, so the EIP-55 checksum must catch it.
        ok, reason = validate_evm("0x5b6EF408c4eBb166788C0cA4cB644f12AC757777")
        assert not ok and reason == "checksum_failed"

    @pytest.mark.parametrize("bad", [
        "0x123",                                       # too short
        "0x5B6Ef408c4eBb166788C0cA4cB644f12AC7577777",  # too long
        "5B6Ef408c4eBb166788C0cA4cB644f12AC757777",     # missing prefix
        "0xZZ6Ef408c4eBb166788C0cA4cB644f12AC757777",   # non-hex
    ])
    def test_bad_shapes(self, bad):
        ok, reason = validate_evm(bad)
        assert not ok and reason == "bad_shape"


class TestSolana:
    def test_valid_mint(self):
        ok, reason = validate_solana("8XkvLLFHBJvkmCQkHfK4pEymsiZkCXdouSCRpZw5Sbj8")
        assert ok and reason == "ok"

    def test_lowercased_address_rejected(self):
        # base58 excludes 0 O I l, so a lowercased address is unrecoverable.
        ok, reason = validate_solana("2zrmpeat65m8p3hzy4epmxvwduvupq1qlr9oq6epins3")
        assert not ok and reason == "bad_base58"

    def test_too_short(self):
        ok, reason = validate_solana("abc")
        assert not ok and reason == "bad_length"

    def test_decodes_too_few_bytes(self):
        # 32 valid base58 chars, but only 23 bytes of value.
        ok, reason = validate_solana("2" * 32)
        assert not ok and reason == "decoded_23_bytes"

    def test_decodes_too_many_bytes(self):
        # In range on length, but 33 bytes once decoded.
        ok, reason = validate_solana("z" * 44)
        assert not ok and reason == "decoded_33_bytes"

    def test_leading_ones_still_count_as_bytes(self):
        # "1" is zero in base58, so 32 of them is a legitimately-shaped
        # 32-byte address (the System Program) rather than a short decode.
        ok, reason = validate_solana("1" * 32)
        assert ok and reason == "ok"


class TestBase58:
    def test_roundtrip_known_value(self):
        assert len(b58decode("8XkvLLFHBJvkmCQkHfK4pEymsiZkCXdouSCRpZw5Sbj8")) == 32

    def test_leading_ones_are_zero_bytes(self):
        assert b58decode("1111") == b"\x00\x00\x00\x00"

    def test_invalid_character(self):
        with pytest.raises(ValueError):
            b58decode("0OIl")


class TestDenylistAndNormalise:
    def test_wsol_denied(self):
        assert is_denied("So11111111111111111111111111111111111111112")

    def test_zero_address_denied(self):
        assert is_denied("0x0000000000000000000000000000000000000000")

    def test_denied_short_circuits_validate(self):
        ok, reason = validate("So11111111111111111111111111111111111111112", CHAIN_SOLANA)
        assert not ok and reason == "denylisted"

    def test_evm_normalises_to_lowercase(self):
        mixed = "0x5B6Ef408c4eBb166788C0cA4cB644f12AC757777"
        assert normalize(mixed, CHAIN_EVM) == mixed.lower()

    def test_solana_normalisation_preserves_case(self):
        addr = "8XkvLLFHBJvkmCQkHfK4pEymsiZkCXdouSCRpZw5Sbj8"
        assert normalize(addr, CHAIN_SOLANA) == addr
