# SPDX-License-Identifier: Apache-2.0
"""Assert scrypt cost factor meets OWASP 2024+ minimum (N=2^17).

Required: SCRYPT_N == 131072  (N=2^17)

This test fails loudly if the cost factor regresses to N=2^14 (16384) or
any value below 2^17, catching silent misconfiguration before deployment.
"""
from __future__ import annotations


def test_scrypt_default_n_is_2_to_the_17() -> None:
    """DEFAULT_N must be 2**17 == 131072 per OWASP 2024+ minimum profile."""
    from lmchat.utils.hashing import DEFAULT_N

    expected = 2**17  # 131072
    assert DEFAULT_N == expected, (
        f"REGRESSION: scrypt DEFAULT_N is {DEFAULT_N} (2^{DEFAULT_N.bit_length()-1}), "
        f"expected {expected} (2^17). "
        f"A value below 2^17 weakens password hashing below the OWASP 2024 minimum. "
        f"See src/lmchat/utils/hashing.py for the scrypt parameters."
    )


def test_scrypt_default_n_not_regression_value() -> None:
    """Explicitly assert DEFAULT_N is not the pre-OWASP regression value 2^14."""
    from lmchat.utils.hashing import DEFAULT_N

    regression_value = 2**14  # 16384 — old minimum, now below OWASP floor
    assert DEFAULT_N != regression_value, (
        f"REGRESSION: scrypt DEFAULT_N == {regression_value} (2^14). "
        f"This is below the OWASP 2024+ minimum of 2^17. "
        f"Update DEFAULT_N in src/lmchat/utils/hashing.py."
    )


def test_scrypt_default_r_is_8() -> None:
    """DEFAULT_R must be 8 (standard OWASP profile)."""
    from lmchat.utils.hashing import DEFAULT_R

    assert DEFAULT_R == 8, (
        f"scrypt DEFAULT_R is {DEFAULT_R}, expected 8. "
        f"r=8 is the OWASP 2024 standard block size for scrypt."
    )


def test_scrypt_default_p_is_1() -> None:
    """DEFAULT_P must be 1 (standard profile; p=1 with N=2^17 is OWASP compliant)."""
    from lmchat.utils.hashing import DEFAULT_P

    assert DEFAULT_P == 1, (
        f"scrypt DEFAULT_P is {DEFAULT_P}, expected 1. "
        f"Update DEFAULT_P in src/lmchat/utils/hashing.py."
    )


def test_scrypt_cost_factor_combined_memory() -> None:
    """Memory cost 128*N*r must be >= 128 MiB (OWASP 2024+ floor)."""
    from lmchat.utils.hashing import DEFAULT_N, DEFAULT_R

    memory_bytes = 128 * DEFAULT_N * DEFAULT_R
    min_bytes = 128 * 1024 * 1024  # 128 MiB

    assert memory_bytes >= min_bytes, (
        f"scrypt memory cost {memory_bytes // (1024*1024)} MiB "
        f"(128 × N={DEFAULT_N} × r={DEFAULT_R}) "
        f"is below the OWASP 2024 minimum of 128 MiB. "
        f"Increase N or r."
    )
