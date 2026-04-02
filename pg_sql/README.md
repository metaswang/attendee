# PostgreSQL with Stunnel SSL Wrapper on myvps2

This setup provides a PostgreSQL database wrapped in an `stunnel` SSL tunnel, mimicking the `myvps:/voxella/redis/` configuration.

Persistent data now lives under `/mnt/HC_Volume_105273825/voxstudio/pg_sql/` so database state survives container recreation:

- `postgres/data/` for PostgreSQL data files
- `stunnel/` for the tunnel config and certificate pair

## Setup Instructions

1.  **Generate SSL Certificate**
    Run the following command in the `pg_sql/stunnel/` directory to generate a self-signed certificate for `pg.voxstudio.me`:

    ```bash
    openssl req -x509 -nodes -newkey rsa:2048 \
      -keyout stunnel.key \
      -out stunnel.pem \
      -days 3650 \
      -subj "/CN=pg.voxstudio.me" \
      -addext "subjectAltName=DNS:pg.voxstudio.me"
    ```

2.  **Configure `.env`**
    Edit `pg_sql/.env` to set your desired `POSTGRES_PASSWORD`.

3.  **Deploy**
    From the `pg_sql/` directory, run:

    ```bash
    docker compose up -d
    ```

## Connection Methods

### Method A: Environment Variables (Standard)

Use these variables in your application (e.g., `.env` file):

```bash
POSTGRES_DB=postgres
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_secure_password
POSTGRES_HOST=pg.voxstudio.me
POSTGRES_PORT=5432
POSTGRES_SSLMODE=require
```

### Method B: Database URL (DB__URL)

Use this URL format (e.g., for `asyncpg` or `SQLAlchemy`):

```bash
postgresql+asyncpg://postgres:your_secure_password@pg.voxstudio.me:5432/postgres
```

## Maintenance

-   **Restart**: `docker compose restart`
-   **Logs**: `docker compose logs -f`
-   **Stop**: `docker compose stop` (will NOT delete data)
-   **Teardown**: `docker compose down` (will NOT delete data)
