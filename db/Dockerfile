FROM postgres:18.6-trixie
# Keep pg_cron compatible with the server in this image. The next PGDG rebuild
# would also upgrade the PostgreSQL server supplied by the base image.
RUN apt-get -o Acquire::Retries=3 -o Acquire::http::No-Cache=true -o APT::Update::Error-Mode=any update \
    && apt-get install -y --no-install-recommends postgresql-18-cron=1.6.8-1.pgdg13+2 \
    && rm -rf /var/lib/apt/lists/*
