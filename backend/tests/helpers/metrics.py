"""Чтение метрик из глобального реестра. Реестр общий на весь прогон: сравнивайте приращения."""

from prometheus_client import REGISTRY


def sample(name: str, **labels: str) -> float:
    """Значение ряда (0, если ряда ещё нет); у счётчика имя с `_total`, у гистограммы `_count`."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


def total(name: str) -> float:
    """Сумма по всем рядам метрики: для проверок вида «ни один счётчик не вырос»."""
    return sum(s.value for metric in REGISTRY.collect() for s in metric.samples if s.name == name)


def label_values(name: str, label: str) -> set[str]:
    """Все значения лейбла у рядов с этим именем."""
    return {
        s.labels[label]
        for metric in REGISTRY.collect()
        for s in metric.samples
        if s.name == name and label in s.labels
    }
