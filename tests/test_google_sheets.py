from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Company, CompanyDirection, CompanyFieldProvenance, Direction, GoogleSheetsConfig, SheetRowMapping
from app.services.google_sheets import COLUMNS, HEADERS, GoogleSheetsSyncService, _retry, discover_schema
from app.services.provenance import apply_field


class FakeWorksheet:
    def __init__(self):
        self.rows = []

    def get_all_values(self):
        return [row[:] for row in self.rows]

    def update(self, *, range_name, values):
        from gspread.utils import a1_to_rowcol
        start, column = a1_to_rowcol(range_name.split(":", 1)[0])
        while len(self.rows) < start:
            self.rows.append([])
        if column == 1 and len(values[0]) > 1:
            self.rows[start - 1] = values[0][:]
        else:
            while len(self.rows[start - 1]) < column:
                self.rows[start - 1].append("")
            self.rows[start - 1][column - 1] = values[0][0]

    def hide_columns(self, *_):
        pass

    def batch_update(self, updates):
        from gspread.utils import a1_to_rowcol
        for update in updates:
            row, column = a1_to_rowcol(update["range"])
            while len(self.rows) < row:
                self.rows.append([])
            while len(self.rows[row - 1]) < column:
                self.rows[row - 1].append("")
            self.rows[row - 1][column - 1] = update["values"][0][0]


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


def test_realistic_second_row_schema_and_email_status() -> None:
    rows = [[], ["Начало общения    Дата", "Наименование клиента", "Область", "Город", "Кол-во филиалов", "Адрес ", "Почта ", "Телефон", "ЛПР", "Почта", "Телефон", "Статус почтовых отправлений", "Действие", "Результат", "Комментарии"]]
    schema = discover_schema(rows)
    assert schema.header_row == 2 and schema.data_row == 3
    assert schema.fields["company_email"] == 7 and schema.fields["decision_maker_email"] == 10
    assert schema.fields["company_phone"] == 8 and schema.fields["decision_maker_phone"] == 11
    assert schema.email_status_column == 12 and schema.leadflow_id_column == 16
    assert "website" not in schema.fields


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
        assert worksheet.rows[1][14] == company.id
        assert worksheet.rows[0][11] == "Сайт"

        worksheet.rows[1][8] = "Тестовый Менеджер"
        worksheet.rows[1][12] = "Связаться позже"
        worksheet.rows[1][13] = "Перезвонить"
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


def test_row_movement_is_found_by_leadflow_id() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction, company, config = setup_company(session)
        worksheet = FakeWorksheet()
        service = GoogleSheetsSyncService(session, config)
        service.sync_direction(direction, worksheet=worksheet)
        worksheet.rows.insert(1, ["manual spacer"])
        company.company_phone = "+7 000 000-00-00"
        result = service.sync_direction(direction, worksheet=worksheet)
        assert result["inserted"] == 0 and result["updated"] == 1
        matches = [row for row in worksheet.rows if len(row) > 14 and row[14] == company.id]
        assert len(matches) == 1 and matches[0][7] == "+7 000 000-00-00"  # unchanged system value updates after row movement


def test_duplicate_leadflow_id_is_rejected() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction, company, config = setup_company(session)
        worksheet = FakeWorksheet()
        service = GoogleSheetsSyncService(session, config)
        service.sync_direction(direction, worksheet=worksheet)
        worksheet.rows.append(worksheet.rows[1][:])
        import pytest
        with pytest.raises(ValueError, match="Duplicate LeadFlow ID"):
            service.sync_direction(direction, worksheet=worksheet)


def test_transient_google_error_is_retried(monkeypatch) -> None:
    import requests
    attempts = 0
    def operation():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise requests.ConnectionError("transient")
        return "ok"
    monkeypatch.setattr("app.services.google_sheets.time.sleep", lambda _: None)
    assert _retry(operation) == "ok" and attempts == 3


def test_google_auth_transport_error_is_retried(monkeypatch) -> None:
    from google.auth.exceptions import TransportError
    attempts = 0
    def operation():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TransportError("temporary")
        return "ok"
    monkeypatch.setattr("app.services.google_sheets.time.sleep", lambda _: None)
    assert _retry(operation) == "ok" and attempts == 2


def test_existing_sheet_gets_website_column_and_preserves_manual_value() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction, company, config = setup_company(session)
        company.website = "https://parser.example"
        worksheet = FakeWorksheet()
        worksheet.rows = [["Наименование клиента", "Город", "Адрес", "LeadFlow ID"]]
        service = GoogleSheetsSyncService(session, config)
        service.sync_direction(direction, worksheet=worksheet)
        assert worksheet.rows[0][4] == "Кол-во филиалов"
        assert worksheet.rows[0][5] == "Сайт"
        assert worksheet.rows[1][5] == "https://parser.example"
        worksheet.rows[1][5] = "https://www.manual.example/"
        service.sync_direction(direction, worksheet=worksheet)
        assert company.website == "https://manual.example"
        assert worksheet.rows[1][5] == "https://www.manual.example/"
        provenance_count = session.query(CompanyFieldProvenance).filter_by(
            company_id=company.id, field="website", discovery_method="manual",
        ).count()
        service.sync_direction(direction, worksheet=worksheet)
        assert session.query(CompanyFieldProvenance).filter_by(
            company_id=company.id, field="website", discovery_method="manual",
        ).count() == provenance_count


def test_unchanged_system_value_can_be_updated_after_snapshot() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction, company, config = setup_company(session)
        company.website = "https://old.example"
        worksheet = FakeWorksheet()
        service = GoogleSheetsSyncService(session, config)
        service.sync_direction(direction, worksheet=worksheet)
        assert worksheet.rows[1][11] == "https://old.example"
        company.website = "https://new.example"
        service.sync_direction(direction, worksheet=worksheet)
        assert worksheet.rows[1][11] == "https://new.example"


def test_manual_value_can_be_changed_again_after_snapshot() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction, company, config = setup_company(session)
        worksheet = FakeWorksheet()
        service = GoogleSheetsSyncService(session, config)
        service.sync_direction(direction, worksheet=worksheet)

        worksheet.rows[1][8] = "Первый менеджер"
        service.sync_direction(direction, worksheet=worksheet)
        assert company.decision_maker_name == "Первый менеджер"

        worksheet.rows[1][8] = "Новый менеджер"
        service.sync_direction(direction, worksheet=worksheet)
        assert company.decision_maker_name == "Новый менеджер"
        assert worksheet.rows[1][8] == "Новый менеджер"

        assert apply_field(
            session, company, "decision_maker_name", "Менеджер из LeadFlow",
            discovery_method="manual", confidence=1.0,
        ) is True
        service.sync_direction(direction, worksheet=worksheet)
        assert worksheet.rows[1][8] == "Менеджер из LeadFlow"
