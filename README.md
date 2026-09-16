# LeadFlow

MVP backend for collecting company leads from Yandex Maps and 2GIS, normalizing
them into `CompanyLead`, and preventing duplicate companies in PostgreSQL.

Implemented:

- FastAPI endpoints for companies, parser jobs, runs, editable campaigns/templates,
  mailboxes, integrations, and dashboard data;
- Yandex Maps adapter over the pinned `dmikhaylov/yamaps_parser` revision;
- 2GIS adapter over the pinned LGPL `Eroloft/parser-2gis-new` revision;
- database-backed deduplication by source ID, website domain, phone, INN, and
  normalized company name plus address;
- exact `limit_new` handling: duplicates do not consume the requested limit;
- parser run status/checkpoint records and cron worker;
- PostgreSQL migration and Docker Compose stack;
- encrypted SMTP/IMAP and AI/Google credentials, automatic and LLM-personalized sending;
- open/click/reply/bounce/unsubscribe tracking and suppression;
- Google Sheets export, replaceable phrase-search provider, and a compact admin UI.

HTML templates support `company_name`, `city`, `category`, `website`, and (for
manual AI sends) `personalized_text`. Passwords and API keys are never returned
by API schemas. Set a strong `SECRET_KEY` before storing any credentials; changing
it later makes existing encrypted values unreadable.

## Start

Copy `.env.example` to `.env`, then run:

```bash
docker compose up -d --build
```

Without a local `.env`, the admin UI/API use development Basic Auth credentials
`admin` / `change-me-before-production`. Override both values before deployment.

### Самостоятельная настройка администратором

- Мастер «Добавить сферу» создаёт направление поиска, шаблон персонального КП,
  обычный шаблон без ИИ и автоматическую рассылку.
- «Данные отправителя» используются только при создании новых черновиков.
  Существующий черновик сохраняет свою подпись до ручного изменения, а
  отправленный HTML/text snapshot остаётся неизменяемым.
- Файлы направления прикладываются к новым персональным и автоматическим
  письмам. Список фиксируется в черновике: изменение файлов направления не
  меняет существующий черновик и отправленную историю.
- Автоматическая рассылка использует только обычный `EmailTemplate` и не
  вызывает DeepSeek. Ручной запуск также соблюдает suppression, cooldown,
  idempotency и дневные лимиты.

Open API documentation at <http://localhost:8000/docs>.

The primary admin workflow is **Directions** at <http://localhost:8000/>. A
direction owns its queries, locations, source switches, one shared `limit_new`,
cron schedule, and Google Sheet tab. Companies remain canonical and are linked
many-to-many through direction associations.

Google Sheets uses `GOOGLE_SHEETS_SPREADSHEET_ID` and either compact service
account JSON in `GOOGLE_SERVICE_ACCOUNT_JSON` or a readable in-container path.
Share the spreadsheet with the JSON credential's `client_email`. The sync pulls
manual business fields first, then updates rows by the hidden `LeadFlow ID`;
it never clears and rewrites the whole tab.

After the initial image build, the regular start command is:

```bash
docker compose up -d
```

Set unique `SECRET_KEY` and `ADMIN_PASSWORD` values before exposing the service.
Unsubscribe URLs use `PUBLIC_BASE_URL`. In production it must be a public HTTPS
origin; startup and send-time checks reject localhost, private addresses, and
plain HTTP. Open-pixel and click-redirect tracking are disabled by default
because they can reduce deliverability. They can be explicitly enabled with
`EMAIL_OPEN_TRACKING_ENABLED=true` and `EMAIL_CLICK_TRACKING_ENABLED=true` after
the sending domain has established a healthy reputation. Every outbound message
contains matching HTML/text unsubscribe links plus RFC 8058 one-click headers.
Schedules use five-field cron expressions in UTC.

Create a parser job:

```bash
curl -X POST http://localhost:8000/api/parser-jobs \
  -H "Content-Type: application/json" \
  -d '{"source":"yandex_maps","category":"храмы","city":"Москва","limit_new":10}'
```

Start it with `POST /api/parser-jobs/{id}/run`, or provide `schedule`, for
example `0 7 * * 1-5`. Source-specific options include:

- both sources: `max_scan`, `enrich_emails`;
- Yandex: `use_grid`, `upstream_email_enrichment`;
- 2GIS: `delay_ms`, `timeout_seconds`, `chrome_binary`.

`limit_new` counts only rows inserted after all database duplicate checks. A
CAPTCHA stops the run as `blocked`; LeadFlow does not attempt to solve or bypass
it. Increase delays or retry later.

Run it using the returned job ID:

```bash
curl -X POST http://localhost:8000/api/parser-jobs/JOB_ID/run
```

## Operational behavior

The source adapter may inspect more records than `limit_new`. LeadFlow stops
only after the requested number of previously unseen companies has been
committed. If the configured `max_scan` is reached first, the run is marked
`exhausted`. CAPTCHA detection stops the run with `blocked`; LeadFlow does not
attempt to bypass it.

Email discovery reads only public HTML pages from the company domain and records
the exact page, method, and confidence. JavaScript and asset files are not
treated as contact sources. Google Sheets is a manager-facing interface; on sync,
manager-edited direction, email, contact person, and block status are imported
before the database state is exported.

Mail-account connection tests validate both SMTP and IMAP authentication. Both
automatic campaigns and one-off personalized sends enforce mailbox daily limits,
suppression, manual blocking, and the global no-repeat rule.
