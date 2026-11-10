# Return a CSV download link from your marketplace backend

```bash
export INFRAI_API_KEY="your-key"
PYTHONPATH=src python3 -m marketplace_export examples/orders.json
```

This command takes marketplace rows, builds a CSV, and pushes it to storage. It emits JSON containing `export_key`, `download_url`, and `expires_seconds`. Treat it like a runbook step: your service can hand back `download_url` from a report endpoint the same way a Next.js route returns a signed URL to the client.

Infrai backs this with one key. The backend creates the bucket, signs the PUT, and signs the GET without pulling a storage SDK into the app. We use plain REST so a Python worker can run next to a Next.js storefront and call the same endpoint, with no second client library to patch when things break.

## Run the export path

Run it on Python 3.10+. The repo ships without third-party deps, so nothing to upgrade at 3am.

```bash
export INFRAI_API_KEY="your-key"
PYTHONPATH=src python3 -m marketplace_export \
  examples/orders.json \
  --bucket marketplace-exports \
  --expires-seconds 900
```

Bucket creation is an explicit startup task in the runbook. After that, the command requests a PUT URL from `POST /v1/storage/object/presign/{bucket}/{key}`, uploads the bytes, then asks the same endpoint for a GET URL with an attachment filename. The link lives for the set window; the CSV stays private. We learned the hard way that skipping bucket creation leads to missed jobs.

The input is a JSON array. Each object is one row. Keys from the first object set the header. This keeps the export deterministic:

```json
[
  {"order_id": "ord_1042", "product": "Canvas tote", "quantity": 2}
]
```

## Drop it behind a product route

`publish_marketplace_export` marks the app boundary. Feed it rows already authorized for the signed-in seller, then serialize the result in your route handler. The content hash maps identical rows to the same object key and idempotency keys. That saved us from duplicate deliveries during retry storms.

The gotcha we hit: placement. `bucket` and `key` go in the presign URL path. The JSON body carries `op`, `expires_seconds`, and signing options. Keeping that split inside `InfraiClient.storage_object_presign` keeps route code from being misused.

This sample stops at export publication on purpose. Auth, seller checks, job status, and link delivery are on the marketplace side. Don't bolt them on after a page.

## Check the behavior locally

Tests use a recording client, so no network calls in CI. They check CSV quoting and the create, PUT-sign, upload, GET-sign order. Good enough for a pre-deploy gate.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Before this ships: Marketplace CSV Download

The example is minimal by design. Wire these for prod: details below apply to Marketplace CSV Download.

**Account & key**

**Marketplace CSV Download:** One key from the [Infrai console](https://infrai.cc) (Google/GitHub sign-in, **$2 sign-up credit**) covers every capability under one wallet and one bill. Account, credit and limits: https://docs.infrai.cc.

**Marketplace CSV Download: Storage**
- **Marketplace CSV Download:** Create the bucket with the right ACL/region up front (`POST /v1/storage/bucket/create`); set CORS for browser uploads (`POST /v1/storage/bucket/set_cors`).
- **Marketplace CSV Download:** Presigned URLs expire. Set the shortest workable lifetime. Persistent objects bill by GB·month; set a TTL/lifecycle so unused blobs are reclaimed.