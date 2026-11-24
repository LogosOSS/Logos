FROM postgres:17.11-trixie
# Keep pg_cron compatible with the server in this image. The next PGDG rebuild
# requires a PostgreSQL minor release that is not yet present in its index.
RUN apt-get -o Acquire::Retries=3 -o Acquire::http::No-Cache=true -o APT::Update::Error-Mode=any update \
    && apt-get install -y --no-install-recommends postgresql-17-cron=1.6.8-1.pgdg13+2 \
    && rm -rf /var/lib/apt/lists/*
