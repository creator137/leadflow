FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CHROME_BINARY=/usr/bin/chromium

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium git ca-certificates fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

RUN printf 'precedence ::ffff:0:0/96  100\n' >> /etc/gai.conf

COPY pyproject.toml requirements.txt README.md THIRD_PARTY_NOTICES.md ./
COPY app/__init__.py ./app/__init__.py
COPY vendor ./vendor
RUN python -m pip install --no-cache-dir \
      'alembic>=1.14,<2' 'apscheduler>=3.10,<4' 'beautifulsoup4>=4.12,<5' \
      'cryptography>=44,<47' 'fastapi>=0.115,<1' 'google-auth>=2.38,<3' \
      'gspread>=6.1,<7' 'httpx>=0.28,<1' 'jinja2>=3.1,<4' 'psycopg[binary]>=3.2,<4' \
      'pydantic-settings>=2.7,<3' 'python-multipart>=0.0.20,<1' \
      'sqlalchemy>=2.0.36,<3' 'uvicorn[standard]>=0.34,<1' 'pytest>=8.3,<9'
RUN python -m pip install --no-cache-dir ./vendor/yamaps_parser
RUN python -m pip install --no-cache-dir ./vendor/parser_2gis_new
RUN python -m pip install --no-cache-dir --no-deps -e . \
    && python -m playwright install chromium

COPY app ./app
COPY tests ./tests
COPY alembic.ini ./
COPY migrations ./migrations
COPY integrations ./integrations

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
