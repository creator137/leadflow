from app.config import Settings
from app.sources.base import SourceAdapter
from app.sources.two_gis import TwoGisAdapter
from app.sources.yandex_maps import YandexMapsAdapter


def build_adapter(source: str, settings: Settings) -> SourceAdapter:
    if source == "yandex_maps":
        return YandexMapsAdapter(
            use_grid=settings.yandex_grid,
            enrich_emails=settings.yandex_enrich_emails,
        )
    if source == "two_gis":
        return TwoGisAdapter(chrome_binary=settings.chrome_binary)
    raise ValueError(f"Unsupported source: {source}")

