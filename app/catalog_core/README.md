# Catalog Core shadow foundation

This package is the Stage 4 Block A skeleton for a future unified Catalog Core.

Current boundary:

- pure DTO, privacy, and draft-validation helpers only;
- no runtime wiring into `app/api/public_catalog.py`, `app/api/catalog_admin.py`, or `app/catalog/reader.py`;
- no imports from `app.dialog`, `app.integrations`, or `app.storage`;
- no Supabase, booking, payment, YooKassa, or YCLIENTS writes;
- no `.env`, database, or local draft file reads/writes.

Future blocks may connect this package to the existing public/admin catalog path
only after focused parity, privacy, and booking-regression tests are in place.
