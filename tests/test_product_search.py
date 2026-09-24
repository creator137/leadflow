from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.models import ProductSearchResult, ProductSearchRun
from app.services.company_enrichment import WebsitePage, WebsiteSnapshot
from app.services.product_search import _price, execute_product_search, product_sheet_config, serialize_product_result


def test_price_extraction_is_evidence_based() -> None:
    assert _price("Стоимость от 12 500 ₽ за тираж") == "от 12 500 ₽"
    assert _price("Цена по запросу") is None


def test_product_search_keeps_supplier_results_outside_crm(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    try:
        run = ProductSearchRun(query="коробка с печатью", city="Москва", use_ai=False, limit=5)
        session.add(run); session.commit()
        monkeypatch.setattr("app.services.product_search.FreeSearchProvider.search", lambda *_: ["https://vendor.example.test/product"])
        page = WebsitePage(
            url="https://vendor.example.test/product", raw_chars=1,
            html='<script type="application/ld+json">{"@type":"Organization","name":"Поставщик","email":"info@vendor.test"}</script>',
            text="Коробка с печатью 40x50. Цена от 1 200 ₽. Телефон +7 495 123-45-67",
        )
        monkeypatch.setattr("app.services.product_search.crawl_website", lambda *_args, **_kwargs: WebsiteSnapshot(pages=[page]))
        result = execute_product_search(session, run, Settings())
        assert result.status == "completed"
        rows = session.query(ProductSearchResult).all()
        assert len(rows) == 1
        assert rows[0].company_name == "Поставщик" and rows[0].price == "от 1 200 ₽"
        assert rows[0].email == "info@vendor.test"
        assert "company_id" not in serialize_product_result(rows[0])
        assert product_sheet_config(session).worksheet_name == "Поиск товаров"
    finally:
        session.close()
