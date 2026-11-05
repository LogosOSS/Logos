---
title: TUM Test Instance
---

# TUM Test Instance

The test instance runs on `logos-test.aet.cit.tum.de`. You need the TUM VPN
and SSH access to the server.

## Web Application and API

Port 443 serves the web application, the Swagger UI and the API. Traefik
routes by path. API paths (`/v1`, `/openai`, `/jobs`, `/api`, `/docs`, and
others) go to the API. All other paths go to the web application.

| Purpose | URL |
| --- | --- |
| Web application | `https://logos-test.aet.cit.tum.de/` |
| Swagger UI | `https://logos-test.aet.cit.tum.de/docs` |

`GET /v1` returns 404. This is correct. Use `/docs` to see the endpoints.
Port 8080 is a TLS alias for the completion API. It is only for clients that
use it already.

## SSH

1. Request [Artemis developer access](https://request.aet.cit.tum.de/).
2. Connect to the TUM VPN.
3. Connect to the server with your TUM ID:

   ```bash
   ssh <tum-id>@logos-test.aet.cit.tum.de
   ```

The Logos installation is in `/opt/logos` on the server.

## Database

PostgreSQL does not accept connections from outside the server. Use an SSH
tunnel.

To use a manual tunnel:

1. Open the tunnel:

   ```bash
   ssh -L 5433:127.0.0.1:5432 <tum-id>@logos-test.aet.cit.tum.de
   ```

2. Connect your database client to `localhost:5433`. Use the values in the
   table below.

| Field | Value |
| --- | --- |
| Database | `logosdb` |
| Username | `postgres` |
| Password | `root` |

To use DBeaver:

1. Create a PostgreSQL connection.
2. On the **SSH** tab, set these values:
   - **Host/IP**: `logos-test.aet.cit.tum.de`
   - **Port**: `22`
   - **User Name**: your TUM ID
   - **Authentication**: Public Key
   - **Private Key**: the path to your SSH private key, for example
     `~/.ssh/id_ed25519`
3. On the **Main** tab, set **Host** to `localhost` and **Port** to `5432`.
4. Set the database, the username and the password from the table above.
