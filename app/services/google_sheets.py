from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

import gspread
import requests
from google.auth.exceptions import TransportError
from google.oauth2.service_account import Credentials
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    Company,
    CompanyDirection,
    CompanyFieldProvenance,
    Direction,
    EmailDelivery,
    GoogleSheetsConfig,
    GoogleSyncRun,
    SheetRowMapping,
)
from app.services.provenance import apply_field
from app.services.secrets import decrypt_secret, encrypt_secret

# New worksheets start with the website address. The first twelve columns are
# the shared business area; manual workflow columns may safely follow it.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("Сайт", "website"), ("Начало общения / Дата", "communication_started_at"), ("Наименование клиента", "company_name"),
    ("Область", "region"), ("Город", "city"), ("Кол-во филиалов", "branches_count"),
    ("Адрес", "address"), ("Почта", "company_email"), ("Телефон", "company_phone"),
    ("ЛПР", "decision_maker_name"), ("Почта", "decision_maker_email"),
    ("Телефон", "decision_maker_phone"),
    ("Действие", "action"), ("Результат?", "result"),
    ("LeadFlow ID", "id"),
)
BUSINESS_COLUMNS = COLUMNS[:12]
HEADERS = [header for header, _ in COLUMNS]
MANUAL_FIELDS = {
    "communication_started_at", "region", "city", "branches_count", "address", "company_email",
    "company_phone", "decision_maker_name", "decision_maker_email", "decision_maker_phone", "website", "inn", "action", "result",
}
T = TypeVar("T")
logger = logging.getLogger(__name__)


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold().replace("?", ""))


def _column_letter(index: int) -> str:
    return gspread.utils.rowcol_to_a1(1, index).rstrip("1")


def _retry(operation: Callable[[], T], attempts: int = 4) -> T:
    for attempt in range(attempts):
        try:
            return operation()
        except (gspread.exceptions.APIError, requests.RequestException, TransportError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", 0)
            transient = isinstance(exc, (requests.RequestException, TransportError)) or status in {429, 500, 502, 503, 504}
            if not transient or attempt == attempts - 1:
                raise
            time.sleep((2 ** attempt) + random.random())
    raise RuntimeError("unreachable")


@dataclass(frozen=True)
class SheetSchema:
    header_row: int
    data_row: int
    fields: dict[str, int]
    leadflow_id_column: int
    email_status_column: int | None


def discover_schema(rows: list[list[str]]) -> SheetSchema:
    header_row = 0
    header: list[str] = []
    for row_number, row in enumerate(rows[:20], start=1):
        normalized = [_normalized(value) for value in row]
        if "наименование клиента" in normalized and ("город" in normalized or "адрес" in normalized):
            header_row, header = row_number, row
            break
    if not header_row:
        raise ValueError("Could not find a header row containing 'Наименование клиента'")

    fields: dict[str, int] = {}
    email_columns: list[int] = []
    phone_columns: list[int] = []
    actions: list[int] = []
    results: list[int] = []
    email_status_column = None
    configured_status = _normalized(get_settings().google_sheets_email_status_header or "")
    for index, raw in enumerate(header, start=1):
        value = _normalized(raw)
        if not value:
            continue
        if value.startswith("начало общения"): fields.setdefault("communication_started_at", index)
        elif value == "наименование клиента": fields.setdefault("company_name", index)
        elif value in {"область", "регион"}: fields.setdefault("region", index)
        elif value == "город": fields.setdefault("city", index)
        elif "кол-во филиалов" in value or "количество филиалов" in value: fields.setdefault("branches_count", index)
        elif value == "адрес": fields.setdefault("address", index)
        elif value == "лпр": fields.setdefault("decision_maker_name", index)
        elif value in {"сайт", "веб-сайт", "website"}: fields.setdefault("website", index)
        elif value == "инн": fields.setdefault("inn", index)
        elif value == "почта": email_columns.append(index)
        elif value == "телефон": phone_columns.append(index)
        elif value == "действие": actions.append(index)
        elif value in {"результат", "результат?"}: results.append(index)
        elif value == "leadflow id": fields.setdefault("id", index)
        if (configured_status and value == configured_status) or ("статус" in value and "почт" in value):
            email_status_column = index
    if email_columns: fields["company_email"] = email_columns[0]
    if len(email_columns) > 1: fields["decision_maker_email"] = email_columns[1]
    if phone_columns: fields["company_phone"] = phone_columns[0]
    if len(phone_columns) > 1: fields["decision_maker_phone"] = phone_columns[1]
    if actions: fields["action"] = actions[0]
    if results: fields["result"] = results[0]
    required = {"company_name", "city", "address"}
    if not required.issubset(fields):
        raise ValueError(f"Invalid mapping; missing fields: {', '.join(sorted(required - fields.keys()))}")
    last_business = max((index for index, value in enumerate(header, 1) if value.strip()), default=len(header))
    leadflow_id_column = fields.get("id", last_business + 1)
    fields["id"] = leadflow_id_column
    return SheetSchema(header_row, header_row + 1, fields, leadflow_id_column, email_status_column)


def _text(value: Any) -> str:
    if value is None: return ""
    if isinstance(value, datetime): return value.isoformat()
    return str(value)


def company_row(company: Company) -> list[str]:
    return [_text(getattr(company, field_name)) for _, field_name in COLUMNS]


def _website_columns(header: list[str]) -> list[int]:
    return [
        index for index, value in enumerate(header, 1)
        if _normalized(value) in {"сайт", "веб-сайт", "website"}
    ]


def _move_column_left(worksheet: Any, source: int, destination: int) -> None:
    if source <= destination:
        return
    body = {"requests": [{"moveDimension": {
        "source": {
            "sheetId": worksheet.id,
            "dimension": "COLUMNS",
            "startIndex": source - 1,
            "endIndex": source,
        },
        "destinationIndex": destination - 1,
    }}]}
    _retry(lambda: worksheet.spreadsheet.batch_update(body))


def standardize_business_columns(worksheet: Any, rows: list[list[str]]) -> list[list[str]]:
    """Keep one Website column at A and align the safe business area A:L.

    Moving/inserting whole columns through the Sheets API preserves formulas,
    formatting and manual workflow data that live to the right of this area.
    """
    schema = discover_schema(rows)
    header = rows[schema.header_row - 1][:]
    website_columns = _website_columns(header)
    if len(website_columns) > 1:
        primary = website_columns[0]
        updates: list[dict[str, Any]] = []
        for row_number, row in enumerate(rows[schema.data_row - 1:], start=schema.data_row):
            current = row[primary - 1].strip() if len(row) >= primary else ""
            if current:
                continue
            replacement = next(
                (row[column - 1].strip() for column in website_columns[1:] if len(row) >= column and row[column - 1].strip()),
                "",
            )
            if replacement:
                updates.append({"range": f"{_column_letter(primary)}{row_number}", "values": [[replacement]]})
        if updates:
            _retry(lambda: worksheet.batch_update(updates))
        for column in reversed(website_columns[1:]):
            _retry(lambda column=column: worksheet.delete_columns(column))
            for row in rows:
                if len(row) >= column:
                    row.pop(column - 1)
        header = rows[schema.header_row - 1][:]

    # A previously interrupted alignment can leave a titled but fully empty
    # contact column. Remove only such empty extras; populated manual columns
    # are never deleted.
    for contact_header in ("почта", "телефон"):
        columns = [index for index, value in enumerate(header, 1) if _normalized(value) == contact_header]
        while len(columns) > 2:
            removable = next((
                column for column in columns
                if not any(len(row) >= column and row[column - 1].strip() for row in rows[schema.data_row - 1:])
            ), None)
            if removable is None:
                break
            _retry(lambda removable=removable: worksheet.delete_columns(removable))
            for row in rows:
                if len(row) >= removable:
                    row.pop(removable - 1)
            header = rows[schema.header_row - 1][:]
            columns = [index for index, value in enumerate(header, 1) if _normalized(value) == contact_header]

    for destination, (title, field_name) in enumerate(BUSINESS_COLUMNS, start=1):
        current_schema = discover_schema([header])
        source = current_schema.fields.get(field_name)
        if source is None:
            empty_destination = (
                destination <= len(header)
                and not header[destination - 1].strip()
                and not any(
                    len(row) >= destination and row[destination - 1].strip()
                    for row in rows[schema.data_row - 1:]
                )
            )
            if not empty_destination:
                _retry(lambda destination=destination: worksheet.insert_cols([[""]], col=destination))
                header.insert(destination - 1, "")
            cell = f"{_column_letter(destination)}{schema.header_row}"
            _retry(lambda cell=cell, title=title: worksheet.update(range_name=cell, values=[[title]]))
            header[destination - 1] = title
        elif source > destination:
            _move_column_left(worksheet, source, destination)
            header.insert(destination - 1, header.pop(source - 1))
        elif source < destination:
            raise ValueError(f"Cannot safely align sheet column {field_name}: {source} -> {destination}")
    header_updates = []
    for column, (title, _) in enumerate(BUSINESS_COLUMNS, start=1):
        if header[column - 1] != title:
            header_updates.append({
                "range": f"{_column_letter(column)}{schema.header_row}",
                "values": [[title]],
            })
            header[column - 1] = title
    if header_updates:
        _retry(lambda: worksheet.batch_update(header_updates))
    return _retry(worksheet.get_all_values)


def _parse_manual(field_name: str, value: str) -> Any:
    if field_name == "communication_started_at":
        try: return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError: return value
    if field_name == "branches_count":
        try: return int(value)
        except ValueError: return value
    if field_name == "website":
        from app.normalize import normalize_website
        return normalize_website(value)
    return value


def worksheet_from_config(config: GoogleSheetsConfig, sheet_tab: str):
    info = json.loads(decrypt_secret(config.credentials_encrypted))
    credentials = Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    client = gspread.authorize(credentials)
    client.http_client.timeout = (10, 30)
    spreadsheet = client.open_by_key(config.spreadsheet_id)
    try:
        return spreadsheet.worksheet(sheet_tab)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=sheet_tab, rows=1000, cols=len(COLUMNS))
        worksheet.update(range_name=f"A1:{_column_letter(len(COLUMNS))}1", values=[HEADERS])
        return worksheet


def active_sheets_config(session: Session, settings: Settings) -> GoogleSheetsConfig | None:
    config = session.scalar(select(GoogleSheetsConfig).where(GoogleSheetsConfig.active.is_(True)).limit(1))
    if config or not settings.google_service_account_json: return config
    raw = settings.google_service_account_json
    info = json.loads(raw) if raw.lstrip().startswith("{") else json.loads(Path(raw).read_text(encoding="utf-8"))
    config = GoogleSheetsConfig(spreadsheet_id=settings.google_sheets_spreadsheet_id, worksheet_name="Directions", credentials_encrypted=encrypt_secret(json.dumps(info)), active=True)
    session.add(config); session.commit(); session.refresh(config)
    return config


class GoogleSheetsSyncService:
    def __init__(self, session: Session, config: GoogleSheetsConfig):
        self.session, self.config = session, config

    def sync_direction(self, direction: Direction, *, worksheet=None) -> dict[str, int]:
        worksheet = worksheet or _retry(lambda: worksheet_from_config(self.config, direction.sheet_tab))
        rows = _retry(worksheet.get_all_values)
        if not rows or not any(any(cell.strip() for cell in row) for row in rows):
            _retry(lambda: worksheet.update(range_name=f"A1:{_column_letter(len(COLUMNS))}1", values=[HEADERS]))
            rows = [HEADERS]
        rows = standardize_business_columns(worksheet, rows)
        schema = discover_schema(rows)
        header = rows[schema.header_row - 1]
        for field_name, field_header in (("branches_count", "Кол-во филиалов"), ("website", "Сайт"), ("inn", "ИНН")):
            if field_name in schema.fields:
                continue
            column = max(len(header), schema.leadflow_id_column) + 1
            if getattr(worksheet, "col_count", column) < column:
                _retry(lambda: worksheet.add_cols(column - worksheet.col_count))
            cell = f"{_column_letter(column)}{schema.header_row}"
            _retry(lambda cell=cell, field_header=field_header: worksheet.update(range_name=cell, values=[[field_header]]))
            while len(header) < column: header.append("")
            header[column - 1] = field_header
            schema.fields[field_name] = column
        if len(header) < schema.leadflow_id_column or _normalized(header[schema.leadflow_id_column - 1]) != "leadflow id":
            cell = f"{_column_letter(schema.leadflow_id_column)}{schema.header_row}"
            _retry(lambda: worksheet.update(range_name=cell, values=[["LeadFlow ID"]]))
            try: worksheet.hide_columns(schema.leadflow_id_column - 1, schema.leadflow_id_column)
            except (AttributeError, gspread.exceptions.APIError): pass
            while len(header) < schema.leadflow_id_column: header.append("")
            header[schema.leadflow_id_column - 1] = "LeadFlow ID"

        companies = list(self.session.scalars(select(Company).join(CompanyDirection, CompanyDirection.company_id == Company.id).where(CompanyDirection.direction_id == direction.id).order_by(CompanyDirection.created_at)))
        by_id = {company.id: company for company in companies}
        row_by_id: dict[str, int] = {}
        for row_number, row in enumerate(rows[schema.data_row - 1:], start=schema.data_row):
            leadflow_id = row[schema.leadflow_id_column - 1].strip() if len(row) >= schema.leadflow_id_column else ""
            if not leadflow_id: continue
            if leadflow_id in row_by_id: raise ValueError(f"Duplicate LeadFlow ID found in rows {row_by_id[leadflow_id]} and {row_number}")
            row_by_id[leadflow_id] = row_number

        mappings = {m.company_id: m for m in self.session.scalars(select(SheetRowMapping).where(SheetRowMapping.direction_id == direction.id, SheetRowMapping.spreadsheet_id == self.config.spreadsheet_id))}
        provenance_rows = self.session.scalars(select(CompanyFieldProvenance).where(
            CompanyFieldProvenance.company_id.in_(by_id.keys()),
            CompanyFieldProvenance.discovery_method == "manual",
        ).order_by(CompanyFieldProvenance.discovered_at.desc())).all()
        latest_manual: dict[tuple[str, str], CompanyFieldProvenance] = {}
        for provenance in provenance_rows:
            latest_manual.setdefault((provenance.company_id, provenance.field), provenance)
        sheet_owned = {
            key for key, provenance in latest_manual.items()
            if (provenance.source_url or "").startswith("google-sheets://")
        }
        imported = 0
        for company_id, row_number in row_by_id.items():
            company = by_id.get(company_id)
            if not company: continue
            row = rows[row_number - 1] if row_number <= len(rows) else []
            snapshot = (mappings.get(company_id).last_synced_values or {}) if mappings.get(company_id) else {}
            for field_name in MANUAL_FIELDS:
                index = schema.fields.get(field_name)
                if not index: continue
                value = row[index - 1].strip() if len(row) >= index else ""
                changed_by_user = bool(value) and (
                    field_name not in snapshot or value != snapshot.get(field_name, "")
                    or (company_id, field_name) in sheet_owned
                )
                parsed_value = _parse_manual(field_name, value)
                needs_manual_owner = (company_id, field_name) not in sheet_owned
                if changed_by_user and (getattr(company, field_name) != parsed_value or needs_manual_owner):
                    imported += int(apply_field(self.session, company, field_name, parsed_value, discovery_method="manual", source_url=f"google-sheets://{self.config.spreadsheet_id}/{direction.sheet_tab}/{row_number}", confidence=1.0))
        self.session.commit()

        # Rows may all shift after a user inserts/sorts rows. Move cached row numbers
        # out of the positive worksheet range first so the unique cache constraint
        # cannot collide while SQLAlchemy flushes individual mapping updates.
        if mappings:
            self.session.execute(update(SheetRowMapping).where(
                SheetRowMapping.direction_id == direction.id,
                SheetRowMapping.spreadsheet_id == self.config.spreadsheet_id,
            ).values(sheet_row=-SheetRowMapping.sheet_row))
            self.session.flush()
            for mapping in mappings.values():
                self.session.refresh(mapping)
        occupied = set(row_by_id.values())
        next_row = schema.data_row
        updates: list[dict[str, Any]] = []
        inserted = updated = 0
        for company in companies:
            row_number = row_by_id.get(company.id)
            if row_number is None:
                if company.id in mappings:
                    raise ValueError(f"LeadFlow ID {company.id} is missing from sheet; refusing to append a duplicate")
                while next_row in occupied or (next_row <= len(rows) and len(rows[next_row - 1]) >= schema.fields["company_name"] and rows[next_row - 1][schema.fields["company_name"] - 1].strip()):
                    next_row += 1
                row_number, next_row, inserted = next_row, next_row + 1, inserted + 1
                occupied.add(row_number)
            else: updated += 1
            existing = rows[row_number - 1] if row_number <= len(rows) else []
            mapping = mappings.get(company.id)
            snapshot = (mapping.last_synced_values or {}) if mapping else {}
            synced_values: dict[str, str] = {}
            for field_name, column in schema.fields.items():
                value = _text(getattr(company, field_name))
                current = existing[column - 1].strip() if len(existing) >= column else ""
                changed_by_user = field_name in MANUAL_FIELDS and bool(current) and (
                    field_name not in snapshot or current != snapshot.get(field_name, "")
                    or (company.id, field_name) in sheet_owned
                )
                if changed_by_user:
                    value = current
                if value != current:
                    updates.append({"range": f"{_column_letter(column)}{row_number}", "values": [[value]]})
                if field_name != "id":
                    synced_values[field_name] = value
            if schema.email_status_column:
                latest = self.session.scalar(select(EmailDelivery.status).where(EmailDelivery.company_id == company.id, EmailDelivery.direction_id == direction.id).order_by(EmailDelivery.created_at.desc()).limit(1)) or ""
                current = existing[schema.email_status_column - 1].strip() if len(existing) >= schema.email_status_column else ""
                if latest != current: updates.append({"range": f"{_column_letter(schema.email_status_column)}{row_number}", "values": [[latest]]})
            if mapping is None:
                mapping = SheetRowMapping(company_id=company.id, direction_id=direction.id, spreadsheet_id=self.config.spreadsheet_id, sheet_tab=direction.sheet_tab, sheet_row=row_number, last_synced_values=synced_values)
                self.session.add(mapping)
            else:
                mapping.sheet_tab, mapping.sheet_row, mapping.last_synced_at, mapping.last_synced_values = direction.sheet_tab, row_number, datetime.now(timezone.utc), synced_values
        if updates: _retry(lambda: worksheet.batch_update(updates))
        self.session.add(GoogleSyncRun(
            config_id=self.config.id, direction_id=direction.id, status="completed",
            rows_inserted=inserted, rows_updated=updated, manual_changes=imported,
            finished_at=datetime.now(timezone.utc),
        ))
        self.session.commit()
        return {"imported": imported, "inserted": inserted, "updated": updated, "total": len(companies)}


def sync_companies(session: Session, config: GoogleSheetsConfig) -> dict[str, int]:
    totals = {"imported": 0, "inserted": 0, "updated": 0, "total": 0}
    service = GoogleSheetsSyncService(session, config)
    for direction in session.scalars(select(Direction).where(Direction.archived_at.is_(None))):
        result = service.sync_direction(direction)
        for key in totals: totals[key] += result[key]
        # The command is read from the actual row by stable LeadFlow ID. It only
        # prepares a draft; sending always requires confirmation in the admin UI.
        from app.services.sheet_personalization import process_sheet_personalization_triggers
        try:
            process_sheet_personalization_triggers(session, config, direction, get_settings())
        except Exception:
            session.rollback()
            logger.exception("Google Sheets personalization trigger failed for direction %s", direction.id)
    return totals
