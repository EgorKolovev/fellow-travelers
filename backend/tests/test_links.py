from urllib.parse import parse_qs, urlsplit

import pytest

from app.links import map_url, yandex_go_url
from app.schemas.common import Point


def pt(lat: float | None = None, lon: float | None = None) -> Point:
    return Point(address="Адрес, не для ссылки", lat=lat, lon=lon)


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def test_map_url_longitude_first() -> None:
    url = map_url(pt(55.97, 37.41))

    assert url == "https://yandex.ru/maps/?pt=37.41,55.97&z=17&l=map"


def test_map_url_none_without_coordinates() -> None:
    assert map_url(pt()) is None


def test_map_url_has_no_address() -> None:
    url = map_url(pt(55.97, 37.41))

    assert url is not None
    assert "Адрес" not in url
    assert "%D0" not in url


@pytest.mark.parametrize(
    ("lat", "lon", "expected"),
    [
        (-90, -180, "-180,-90"),
        (90, 180, "180,90"),
        (0, 0, "0,0"),
        (-33.8688, 151.2093, "151.2093,-33.8688"),
    ],
)
def test_map_url_negative_and_boundary(lat: float, lon: float, expected: str) -> None:
    url = map_url(pt(lat, lon))

    assert url is not None
    assert query(url)["pt"] == expected


def test_number_format_max_six_decimals_no_exponent() -> None:
    url = map_url(pt(55.123456789, 0.0000001))

    assert url is not None
    assert query(url)["pt"] == "0,55.123457"
    assert "e" not in query(url)["pt"].lower()


def test_negative_zero_is_plain_zero() -> None:
    url = map_url(pt(-0.0, -0.0000001))

    assert url is not None
    assert query(url)["pt"] == "0,0"


def test_yandex_go_url_has_both_points() -> None:
    url = yandex_go_url(pt(55.9, 37.4), pt(55.75, 37.6))

    assert url is not None
    assert url.startswith("https://3.redirect.appmetrica.yandex.com/route?")
    q = query(url)
    assert (q["start-lat"], q["start-lon"]) == ("55.9", "37.4")
    assert (q["end-lat"], q["end-lon"]) == ("55.75", "37.6")
    assert q["ref"] == "fellowtravelers"
    assert q["appmetrica_tracking_id"]


@pytest.mark.parametrize(
    ("start", "end"),
    [(pt(), pt(1, 2)), (pt(1, 2), pt()), (pt(), pt())],
)
def test_yandex_go_url_none_if_any_point_lacks_coordinates(start: Point, end: Point) -> None:
    assert yandex_go_url(start, end) is None


def test_yandex_go_url_negative_and_boundary() -> None:
    url = yandex_go_url(pt(-90, -180), pt(90, 180))

    assert url is not None
    q = query(url)
    assert (q["start-lat"], q["start-lon"]) == ("-90", "-180")
    assert (q["end-lat"], q["end-lon"]) == ("90", "180")


def test_yandex_go_url_has_no_address() -> None:
    url = yandex_go_url(pt(1, 2), pt(3, 4))

    assert url is not None
    assert "%D0" not in url
