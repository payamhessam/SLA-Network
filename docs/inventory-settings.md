# Settings and Device Inventory

## Scope

The Settings expansion adds database-backed site codes, zones, device-type/role mappings, controlled device naming, local inventory, read-only LogicMonitor lookup, and staged bulk imports. LogicMonitor requests remain GET-only.

## Naming

Names are generated as `[SITE CODE]-[ZONE]-[DEVICE TYPE]-[DEVICE NUMBER]`. City is derived from `Site`; role is derived from `InventoryDeviceType`; device numbers are stored as two digits. Generated name, management IP, LogicMonitor device ID, and the four-part naming combination are unique.

## Seed data

The administrator-supplied Canadian site list is inserted only when the sites table is empty. Zones Z01–Z09 and DSW/ASW/RTR/AP mappings are seeded the same way. Thereafter these are local database records managed through Settings; they are not hard-coded selection lists in the browser.

## API and permissions

Inventory and controlled-list GET endpoints require authentication. Mutations require Administrator. The current two-account deployment maps `admin` to Administrator and `user` to Read-Only Viewer. LogicMonitor lookup and refresh only call the protected GET client.

## Bulk import

The downloadable workbook contains Instructions, Device Inventory, Site Codes, and Valid Values worksheets. Uploads accept UTF-8 CSV or XLSX, are limited to 10 MB and 5,000 rows, neutralize spreadsheet-formula prefixes, and produce a stored validation job before commit. `valid_rows_only` and `all_or_nothing` commit modes are supported.

## Operations

Build and start with both Compose files so secrets are mounted:

`docker compose -f docker-compose.yml -f docker-compose.production.yml up -d --build`

Database tables are created idempotently during startup. For future destructive schema changes, introduce Alembic migrations rather than changing existing columns in place.

## Verification

The isolated API tests cover controlled seeds, automatic derivation, four acceptance names, duplicate name/IP rejection, permissions, workbook delivery, validation preview, and transactional import modes. The Docker production build compiles TypeScript and runs Nginx/API/PostgreSQL as non-root hardened services.
