from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import gspread
from google.oauth2.service_account import Credentials
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Company, CompanyDirection, Direction, EmailDelivery, GoogleSheetsConfig, SheetRowMapping
from app.config import Settings, get_settings
from app.services.provenance import apply_field
from app.services.secrets import decrypt_secret, encrypt_secret

COLUMNS: tuple[tuple[str, str], ...] = (
    ("Начало общения / Дата", "communication_started_at"),
    ("Наименование клиента", "company_name"),
    ("Область", "region"),
    ("Город", "city"),
    ("Кол-во филиалов", "branches_count"),
    ("Адрес", "address"),
    ("Почта", "company_email"),
    ("Телефон", "company_phone"),
    ("ЛПР", "decision_maker_name"),
    ("Почта", "decision_maker_email"),
    ("Телефон", "decision_maker_phone"),
    ("Действие", "action"),
    ("Результат?", "result"),
    ("LeadFlow ID", "id"),
)
HEADERS = [header for header, _ in COLUMNS]
MANUAL_COLUMNS = {
    0: "communication_started_at",
    8: "decision_maker_name",
    9: "decision_maker_email",
    10: "decision_maker_phone",
    11: "action",
    12: "result",
}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def company_row(company: Company) -> list[str]:
    return [_text(getattr(company, field_name)) for _, field_name in COLUMNS]


def _parse_manual(field_name: str, value: str) -> Any:
    if field_name == "communication_started_at":
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return value


def worksheet_from_config(config: GoogleSheetsConfig, sheet_tab: str):
    info = json.loads(decrypt_secret(config.credentials_encrypted))
    credentials = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    spreadsheet = gspread.authorize(credentials).open_by_key(config.spreadsheet_id)
    try:
        return spreadsheet.worksheet(sheet_tab)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=sheet_tab, rows=1000, cols=len(COLUMNS))


def active_sheets_config(session: Session, settings: Settings) -> GoogleSheetsConfig | None:
    config = session.scalar(select(GoogleSheetsConfig).where(GoogleSheetsConfig.active.is_(True)).limit(1))
    if config or not settings.google_service_account_json:
        return config
    raw = settings.google_service_account_json
    info = json.loads(raw) if raw.lstrip().startswith("{") else json.loads(Path(raw).read_text(encoding="utf-8"))
    config = GoogleSheetsConfig(
        spreadsheet_id=settings.google_sheets_spreadsheet_id,
        worksheet_name="Directions",
        credentials_encrypted=encrypt_secret(json.dumps(info)),
        active=True,
    )
    session.add(config)
    session.commit()
    session.refresh(config)
    return config


class GoogleSheetsSyncService:
    def __init__(self, session: Session, config: GoogleSheetsConfig):
        self.session = session
        self.config = config

    def sync_direction(self, direction: Direction, *, worksheet=None) -> dict[str, int]:
        worksheet = worksheet or worksheet_from_config(self.config, direction.sheet_tab)
        rows = worksheet.get_all_values()
        if not rows:
            worksheet.update(range_name="A1:N1", values=[HEADERS])
            rows = [HEADERS]
            try:
                worksheet.hide_columns(13, 14)
            except (AttributeError, gspread.exceptions.APIError):
                pass
        elif rows[0][:len(HEADERS)] != HEADERS:
            raise ValueError(f"Sheet {direction.sheet_tab!r} has an incompatible header row")
        status_header = get_settings().google_sheets_email_status_header
        status_index = rows[0].index(status_header) if status_header and status_header in rows[0] else None

        companies = list(self.session.scalars(
            select(Company).join(CompanyDirection, CompanyDirection.company_id == Company.id).where(
                CompanyDirection.direction_id == direction.id,
            ).order_by(CompanyDirection.created_at)
        ))
        by_id = {company.id: company for company in companies}
        row_by_id: dict[str, int] = {}
        for row_number, row in enumerate(rows[1:], start=2):
            leadflow_id = row[13].strip() if len(row) > 13 else ""
            if leadflow_id and leadflow_id not in row_by_id:
                row_by_id[leadflow_id] = row_number

        imported = 0
        for row_number, row in enumerate(rows[1:], start=2):
            leadflow_id = row[13].strip() if len(row) > 13 else ""
            company = by_id.get(leadflow_id)
            if not company:
                continue
            for index, field_name in MANUAL_COLUMNS.items():
                value = row[index].strip() if len(row) > index else ""
                if value and _text(getattr(company, field_name)) != value:
                    imported += int(apply_field(
                        self.session, company, field_name, _parse_manual(field_name, value),
                        discovery_method="manual",
                        source_url=f"google-sheets://{self.config.spreadsheet_id}/{direction.sheet_tab}/{row_number}",
                        confidence=1.0,
                    ))
        self.session.commit()

        inserted = updated = 0
        next_row = max(len(rows) + 1, 2)
        for company in companies:
            row_number = row_by_id.get(company.id)
            if row_number is None:
                row_number = next_row
                next_row += 1
                inserted += 1
            else:
                updated += 1
            output = company_row(company)
            end_column = "N"
            if status_index is not None:
                while len(output) <= status_index:
                    output.append("")
                latest_status = self.session.scalar(select(EmailDelivery.status).where(
                    EmailDelivery.company_id == company.id, EmailDelivery.direction_id == direction.id,
                ).order_by(EmailDelivery.created_at.desc()).limit(1))
                output[status_index] = latest_status or ""
                end_column = gspread.utils.rowcol_to_a1(1, len(output)).rstrip("1")
            worksheet.update(range_name=f"A{row_number}:{end_column}{row_number}", values=[output])
            mapping = self.session.scalar(select(SheetRowMapping).where(
                SheetRowMapping.company_id == company.id,
                SheetRowMapping.direction_id == direction.id,
                SheetRowMapping.spreadsheet_id == self.config.spreadsheet_id,
            ))
            if mapping is None:
                mapping = SheetRowMapping(
                    company_id=company.id,
                    direction_id=direction.id,
                    spreadsheet_id=self.config.spreadsheet_id,
                    sheet_tab=direction.sheet_tab,
                    sheet_row=row_number,
                )
                self.session.add(mapping)
            else:
                mapping.sheet_tab = direction.sheet_tab
                mapping.sheet_row = row_number
                mapping.last_synced_at = datetime.now(timezone.utc)
        self.session.commit()
        return {"imported": imported, "inserted": inserted, "updated": updated, "total": len(companies)}


def sync_companies(session: Session, config: GoogleSheetsConfig) -> dict[str, int]:
    totals = {"imported": 0, "inserted": 0, "updated": 0, "total": 0}
    directions = list(session.scalars(select(Direction).where(Direction.archived_at.is_(None))))
    service = GoogleSheetsSyncService(session, config)
    for direction in directions:
        result = service.sync_direction(direction)
        for key in totals:
            totals[key] += result[key]
    return totals
