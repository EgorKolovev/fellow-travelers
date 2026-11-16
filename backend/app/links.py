"""Ссылки на Яндекс Карты и Яндекс Go. Чистые функции: без БД и без FastAPI.

В ссылки попадают только координаты: адрес и персональные данные не передаются.
"""

from urllib.parse import urlencode

from app.schemas.common import Point

_MAPS_URL = "https://yandex.ru/maps/"
_MAP_ZOOM = 17
_GO_URL = "https://3.redirect.appmetrica.yandex.com/route"
# Идентификатор редиректа Яндекс Go: без установленного приложения ведёт в магазин приложений.
_GO_TRACKING_ID = "1178268795219780156"
_GO_REF = "fellowtravelers"


def _num(value: float) -> str:
    """Не больше 6 знаков после запятой, без экспоненты и без хвостовых нулей."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def map_url(point: Point) -> str | None:
    """Ссылка на Яндекс Карты с меткой в точке; `None`, если у точки нет координат.

    Формат: `https://yandex.ru/maps/?pt=<lon>,<lat>&z=17&l=map` (долгота, затем широта).
    Документация: https://yandex.ru/dev/yandex-apps-launch/maps/doc/concepts/yandexmaps-web.html
    """
    if point.lat is None or point.lon is None:
        return None
    query = urlencode(
        {"pt": f"{_num(point.lon)},{_num(point.lat)}", "z": _MAP_ZOOM, "l": "map"}, safe=","
    )
    return f"{_MAPS_URL}?{query}"


def yandex_go_url(start: Point, end: Point) -> str | None:
    """Ссылка на заказ такси в Яндекс Go из `start` в `end`.

    `None`, если у одной из точек нет координат.

    Документация: https://yandex.com/support/taxi-distr/ru/api/deeplinks
    (раздел «Формирование ссылок»: `start-lat`, `start-lon`, `end-lat`, `end-lon`,
    `ref`, `appmetrica_tracking_id`).
    """
    if start.lat is None or start.lon is None or end.lat is None or end.lon is None:
        return None
    query = urlencode(
        {
            "start-lat": _num(start.lat),
            "start-lon": _num(start.lon),
            "end-lat": _num(end.lat),
            "end-lon": _num(end.lon),
            "ref": _GO_REF,
            "appmetrica_tracking_id": _GO_TRACKING_ID,
        }
    )
    return f"{_GO_URL}?{query}"
