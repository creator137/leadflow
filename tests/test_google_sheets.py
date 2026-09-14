from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Company, CompanyDirection, CompanyFieldProvenance, Direction, GoogleSheetsConfig, SheetRowMapping
from app.services.google_sheets import (
    BUSINESS_COLUMNS, COLUMNS, HEADERS, SHARED_SHEET_HEADERS, GoogleSheetsSyncService, _retry,
    discover_schema, standardize_business_columns,
)
from app.services.provenance import apply_field


class FakeWorksheet:
    def __init__(self):
        self.rows = []
        self.id = 1
        self.spreadsheet = self

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
        if isinstance(updates, dict):
            move = updates["requests"][0]["moveDimension"]
            source = move["source"]["startIndex"]
            destination = move["destinationIndex"]
            for row in self.rows:
                while len(row) <= source:
                    row.append("")
                row.insert(destination, row.pop(source))
            return
        from gspread.utils import a1_to_rowcol
        for update in updates:
            row, column = a1_to_rowcol(update["range"])
            while len(self.rows) < row:
                self.rows.append([])
            while len(self.rows[row - 1]) < column:
                self.rows[row - 1].append("")
            self.rows[row - 1][column - 1] = update["values"][0][0]

    def delete_columns(self, column):
        for row in self.rows:
            if len(row) >= column:
                row.pop(column - 1)

    def insert_cols(self, values, col=1):
        for row_number, row in enumerate(self.rows, 1):
            value = values[row_number - 1][0] if row_number <= len(values) and values[row_number - 1] else ""
            row.insert(col - 1, value)


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
    assert COLUMNS[0][1] == "website"
    assert COLUMNS[7][1] == "company_email"
    assert COLUMNS[10][1] == "decision_maker_email"
    assert COLUMNS[8][1] == "company_phone"
    assert COLUMNS[11][1] == "decision_maker_phone"
    assert SHARED_SHEET_HEADERS[0] == "Сайт"
    assert SHARED_SHEET_HEADERS[12:] == (
        "Статус почтовых отправлений",
        "Действие", "Результат", "Задача",
        "Действие", "Результат", "Задача",
        "Действие", "Результат", "Задача",
        "ИТОГ", "Комментарии", "LeadFlow ID", "ИНН",
        "Статус персонального КП", "Предпросмотр КП",
    )


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
        id_column = worksheet.rows[0].index("LeadFlow ID")
        assert worksheet.rows[1][id_column] == company.id
        assert worksheet.rows[0][0] == "Сайт"

        worksheet.rows[1][9] = "Тестовый Менеджер"
        action_column = worksheet.rows[0].index("Действие")
        result_column = worksheet.rows[0].index("Результат")
        worksheet.rows[1][action_column] = "Связаться позже"
        worksheet.rows[1][result_column] = "Перезвонить"
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
        id_column = worksheet.rows[0].index("LeadFlow ID")
        matches = [row for row in worksheet.rows if len(row) > id_column and row[id_column] == company.id]
        assert len(matches) == 1 and matches[0][8] == "+7 000 000-00-00"  # unchanged system value updates after row movement


def test_ai_enrichment_updates_same_sheet_row_only_when_empty() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction, company, config = setup_company(session)
        worksheet = FakeWorksheet()
        service = GoogleSheetsSyncService(session, config)
        service.sync_direction(direction, worksheet=worksheet)
        assert worksheet.rows[1][5] == ""
        assert apply_field(session, company, "branches_count", 4, discovery_method="ai_website_analysis",
                           source_url="https://example.test/offices", confidence=0.8)
        result = service.sync_direction(direction, worksheet=worksheet)
        assert result["inserted"] == 0 and result["updated"] == 1
        id_column = worksheet.rows[0].index("LeadFlow ID")
        assert len([row for row in worksheet.rows if len(row) > id_column and row[id_column] == company.id]) == 1
        assert worksheet.rows[1][5] == "4"
        worksheet.rows[1][5] = "7"
        service.sync_direction(direction, worksheet=worksheet)
        assert company.branches_count == 7 and worksheet.rows[1][5] == "7"


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
        assert worksheet.rows[0][0] == "Сайт"
        assert worksheet.rows[0][5] == "Кол-во филиалов"
        assert worksheet.rows[1][0] == "https://parser.example"
        worksheet.rows[1][0] = "https://www.manual.example/"
        service.sync_direction(direction, worksheet=worksheet)
        assert company.website == "https://manual.example"
        assert worksheet.rows[1][0] == "https://www.manual.example/"
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
        assert worksheet.rows[1][0] == "https://old.example"
        company.website = "https://new.example"
        service.sync_direction(direction, worksheet=worksheet)
        assert worksheet.rows[1][0] == "https://new.example"


def test_existing_early_website_column_wins_and_duplicate_is_removed() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction, company, config = setup_company(session)
        worksheet = FakeWorksheet()
        worksheet.rows = [
            ["Начало общения / Дата", "Наименование клиента", "Область", "Город", "Кол-во филиалов",
             "Адрес", "Почта", "Телефон", "Сайт", "ЛПР", "Почта", "Телефон", "Действие",
             "Результат?", "LeadFlow ID", "Сайт"],
            ["", company.company_name, "", company.city, "", company.address, company.company_email,
             company.company_phone, "", "", "", "", "", "", company.id, "https://saved.example"],
        ]

        GoogleSheetsSyncService(session, config).sync_direction(direction, worksheet=worksheet)

        assert worksheet.rows[0].count("Сайт") == 1
        assert worksheet.rows[0][0] == "Сайт"
        assert worksheet.rows[1][0] == "https://saved.example"
        assert company.website == "https://saved.example"


def test_legacy_second_row_header_is_aligned_without_touching_title_row() -> None:
    worksheet = FakeWorksheet()
    worksheet.rows = [
        ["Кафе"],
        ["Начало общения    Дата", "Наименование клиента", "Регион", "Город", "Адрес ",
         "Почта", "Телефон", "Сайт", "ЛПР", "Почта", "Телефон", "Действие", "Результат"],
    ]

    rows = standardize_business_columns(worksheet, worksheet.get_all_values())

    assert "Кафе" in rows[0][:12]
    assert rows[1][:12] == [title for title, _ in BUSINESS_COLUMNS]


def test_interrupted_alignment_reuses_blank_and_removes_empty_contact_duplicate() -> None:
    worksheet = FakeWorksheet()
    worksheet.rows = [
        [],
        ["Сайт", "Начало общения / Дата", "Наименование клиента", "Область", "Город", "",
         "Адрес", "Почта", "Почта", "Телефон", "ЛПР", "Почта", "Телефон", "Действие"],
        ["https://example.test", "", "Компания", "", "Москва", "", "Адрес", "", "info@example.test",
         "+70000000000", "Иван", "director@example.test", "+71111111111", ""],
    ]

    rows = standardize_business_columns(worksheet, worksheet.get_all_values())

    assert rows[1][:12] == [title for title, _ in BUSINESS_COLUMNS]
    assert rows[2][0] == "https://example.test"
    assert rows[2][7] == "info@example.test"
    assert rows[2][8] == "+70000000000"


def test_manual_value_can_be_changed_again_after_snapshot() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction, company, config = setup_company(session)
        worksheet = FakeWorksheet()
        service = GoogleSheetsSyncService(session, config)
        service.sync_direction(direction, worksheet=worksheet)

        worksheet.rows[1][9] = "Первый менеджер"
        service.sync_direction(direction, worksheet=worksheet)
        assert company.decision_maker_name == "Первый менеджер"

        worksheet.rows[1][9] = "Новый менеджер"
        service.sync_direction(direction, worksheet=worksheet)
        assert company.decision_maker_name == "Новый менеджер"
        assert worksheet.rows[1][9] == "Новый менеджер"

        assert apply_field(
            session, company, "decision_maker_name", "Менеджер из LeadFlow",
            discovery_method="manual", confidence=1.0,
        ) is True
        service.sync_direction(direction, worksheet=worksheet)
        assert worksheet.rows[1][9] == "Менеджер из LeadFlow"
