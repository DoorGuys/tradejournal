# Deployment Notes

This app now runs as a Flask web app with account-based journals.

## Local Run

```powershell
.\.venv\Scripts\python.exe futures_platform.py
```

On first local run, existing `trades.json` and `api_settings.json` are migrated into a development account:

- Email: `owner@example.com`
- Password: `ChangeMe123!`

Set `DEV_MIGRATION_EMAIL` and `DEV_MIGRATION_PASSWORD` before first run if you want different credentials.

## Hosting

Use a Python host that can run Flask and persist `futures_platform.db`.

### Render

Build command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
gunicorn futures_platform:app
```

Environment variables:

- `SECRET_KEY`: set a long random secret.
- `DATABASE_URL`: set this to your Render Postgres internal database URL.
- `DATABASE_PATH`: only used for local SQLite development if `DATABASE_URL` is not set.
- `SKIP_LOCAL_MIGRATION=1`: recommended for production.

Important: Render's normal filesystem is ephemeral. For production, use Render Postgres by setting `DATABASE_URL`; do not rely on the SQLite file.

### PythonAnywhere

PythonAnywhere is a good free starting point because the SQLite database file can persist in your account storage. Create a Flask web app and point the WSGI file at `futures_platform.app`.

## What Changed

- Signup, login, and logout are handled by Flask.
- Passwords are stored as hashes, not plain text.
- Each user has a private snapshot containing trades, rules, conditions, and API settings.
- CSV import should be done through the browser upload flow; a hosted server cannot scan a user's local Downloads folder.
