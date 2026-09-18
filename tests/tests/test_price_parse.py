from __future__ import annotations

import pytest

from bot.services.price_parse import parse_price_label_to_minor


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("5 000", 500_000),
        ("5000 ₸", 500_000),
        ("12 500 тг", 1_250_000),
        ("15000", 1_500_000),
        (None, None),
        ("", None),
        ("   ", None),
        ("бесплатно", None),
        ("от 3000", 300_000),
    ],
)
def test_parse_price_label_to_minor(label: str | None, expected: int | None) -> None:
    assert parse_price_label_to_minor(label) == expected
