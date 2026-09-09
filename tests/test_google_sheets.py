from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Company, CompanyDirection, CompanyFieldProvenance, Direction, GoogleSheetsConfig, SheetRowMapping
from app.services.google_sheets import COLUMNS, HEADERS, GoogleSheetsSyncService
from app.services.provenance import apply_field


class FakeWorksheet:
    def __init__(self):
        self.rows = []

    def get_all_values(self):
        return [row[:] for row in self.rows]

    def update(self, *, range_name, values):
        start = int(range_name.split(":", 1)[0][1:])
        while len(self.rows) < start:
            self.rows.append([])
        self.rows[start - 1] = values[0][:]

    def hide_columns(self, *_):
        pass


def setup_company(session: Session):
    direction = Direction(name="Рестораны", slug="рестораны", sheet_tab="Рестораны", limit_new=10)
    company = Company(
        source="yandex_maps", company_name="Ресторан ABC", city="Москва", address="Ленина, 1",
        email="info@abc.ru", company_email="info@abc.ru", phone="+7 999 111-22-33",
        company_phone="+7 999 111-22-33", normalized_name="ресторан abc", raw_data={},
    )
    session.add_all([direction, company])
    session.flush()
    session.add(CompanyDirection(company_id=company.id, direction_id=direction.id))
    config = GoogleSheetsConfig(spreadsheet_id="sheet", worksheet_name="unused", credentials_encrypted="unused")
    session.add(config)
    session.commit()
    return direction, company, config


def test_duplicate_headers_are_mapped_by_position() -> None:
    assert HEADERS.count("Почта") == 2 and HEADERS.count("Телефон") == 2
    assert COLUMNS[6][1] == "company_email"
    assert COLUMNS[9][1] == "decision_maker_email"
    assert COLUMNS[7][1] == "company_phone"
    assert COLUMNS[10][1] == "decision_maker_phone"


def test_sheet_identity_update_and_manual_preservation() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction, company, config = setup_company(session)
        worksheet = FakeWorksheet()
        service = GoogleSheetsSyncService(session, config)
        first = service.sync_direction(direction, worksheet=worksheet)
        assert first == {"imported": 0, "inserted": 1, "updated": 0, "total": 1}
        assert len(worksheet.rows) == 2
        assert worksheet.rows[1][13] == company.id

        worksheet.rows[1][8] = "Тестовый Менеджер"
        worksheet.rows[1][11] = "Связаться позже"
        worksheet.rows[1][12] = "Перезвонить"
        second = service.sync_direction(direction, worksheet=worksheet)
        assert second["inserted"] == 0 and second["updated"] == 1
        assert len(worksheet.rows) == 2
        assert (company.decision_maker_name, company.action, company.result) == (
            "Тестовый Менеджер", "Связаться позже", "Перезвонить",
        )
        assert session.query(SheetRowMapping).count() == 1

        assert apply_field(
            session, company, "decision_maker_name", "Выдуманный AI",
            discovery_method="ai_website_analysis", source_url="https://example.test/team", confidence=0.9,
        ) is False
        assert company.decision_maker_name == "Тестовый Менеджер"
        assert session.scalar(select(CompanyFieldProvenance.discovery_method).where(
            CompanyFieldProvenance.company_id == company.id,
            CompanyFieldProvenance.field == "decision_maker_name",
        )) == "manual"
