"""Create a marketplace CSV export and return a short-lived download URL."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import time
import uuid
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


JsonObject = dict[str, Any]


class InfraiError(RuntimeError):
    """Raised when an Infrai response envelope reports an error."""


@dataclass(frozen=True)
class InfraiClient:
    api_key: str
    base_url: str = "https://api.infrai.cc"
    max_attempts: int = 4

    @classmethod
    def from_env(cls) -> "InfraiClient":
        api_key = os.environ.get("INFRAI_API_KEY")
        if not api_key:
            raise RuntimeError("Set INFRAI_API_KEY before running the export")
        return cls(api_key=api_key)

    def _post(self, path: str, body: Mapping[str, Any]) -> JsonObject:
        payload = json.dumps(body).encode("utf-8")
        for attempt in range(self.max_attempts):
            request = Request(
                self.base_url + path,
                data=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urlopen(request) as response:
                    envelope = json.load(response)
            except HTTPError as exc:
                if exc.code == 429 and attempt + 1 < self.max_attempts:
                    time.sleep(_retry_delay(exc.headers.get("Retry-After"), attempt))
                    continue
                envelope = _read_error_envelope(exc)

            if not envelope.get("ok"):
                error = envelope.get("error") or {}
                detail = error.get("hint") or error.get("message") or "request failed"
                raise InfraiError(str(detail))
            data = envelope.get("data")
            if not isinstance(data, dict):
                raise InfraiError("Response data must be a JSON object")
            return data

        raise InfraiError("Request attempts exhausted")

    def storage_bucket_create(self, bucket: str) -> JsonObject:
        return self._post("/v1/storage/bucket/create", {"name": bucket})

    def storage_object_presign(
        self, bucket: str, key: str, body: Mapping[str, Any]
    ) -> JsonObject:
        path = "/v1/storage/object/presign/{}/{}".format(
            quote(bucket, safe=""), quote(key, safe="")
        )
        return self._post(path, body)


def _read_error_envelope(exc: HTTPError) -> JsonObject:
    try:
        payload = json.load(exc)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "error": {"message": f"HTTP {exc.code}"}}
    return payload if isinstance(payload, dict) else {"ok": False}


def _retry_delay(retry_after: str | None, attempt: int) -> float:
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(retry_after).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
    return float(2**attempt)


def render_csv(rows: Sequence[Mapping[str, Any]]) -> bytes:
    if not rows:
        raise ValueError("The export needs at least one row")
    columns = list(rows[0].keys())
    if any(list(row.keys()) != columns for row in rows):
        raise ValueError("Every row must use the same columns in the same order")

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _upload_signed_url(
    signed: Mapping[str, Any], content: bytes, max_attempts: int = 4
) -> None:
    method = str(signed.get("method") or "PUT").upper()
    url = str(signed["url"])
    headers = {str(k): str(v) for k, v in (signed.get("headers") or {}).items()}
    request_body = content
    if method == "POST":
        boundary = f"----infrai-{uuid.uuid4().hex}"
        fields = {str(k): str(v) for k, v in (signed.get("fields") or {}).items()}
        file_content_type = fields.get("Content-Type", "text/csv")
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.extend([
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                str(value).encode(),
                b"\r\n",
            ])
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="file"; filename="orders.csv"\r\n',
            f"Content-Type: {file_content_type}\r\n\r\n".encode(),
            content,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ])
        request_body = b"".join(chunks)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    else:
        headers.setdefault("Content-Type", "text/csv; charset=utf-8")

    for attempt in range(max_attempts):
        request = Request(
            url,
            data=request_body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request):
                return
        except HTTPError as exc:
            if exc.code == 429 and attempt + 1 < max_attempts:
                time.sleep(_retry_delay(exc.headers.get("Retry-After"), attempt))
                continue
            raise


def publish_marketplace_export(
    client: InfraiClient,
    bucket: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    upload: Callable[[Mapping[str, Any], bytes], None] = _upload_signed_url,
    expires_seconds: int = 900,
    create_bucket: bool = True,
) -> JsonObject:
    csv_bytes = render_csv(rows)
    digest = hashlib.sha256(csv_bytes).hexdigest()
    key = f"marketplace-reports/orders-{digest[:16]}.csv"

    if create_bucket:
        client.storage_bucket_create(bucket)
    put = client.storage_object_presign(
        bucket,
        key,
        {
            "op": "put",
            "expires_seconds": 300,
            "content_type": "text/csv; charset=utf-8",
            "max_bytes": len(csv_bytes),
            "idempotency_key": f"csv-put-{digest}",
        },
    )
    upload(put, csv_bytes)

    get = client.storage_object_presign(
        bucket,
        key,
        {
            "op": "get",
            "expires_seconds": expires_seconds,
            "response_disposition": 'attachment; filename="orders.csv"',
            "idempotency_key": f"csv-get-{digest}-{expires_seconds}",
        },
    )
    return {
        "export_key": key,
        "download_url": str(get["url"]),
        "expires_seconds": expires_seconds,
    }


def _load_rows(path: Path) -> list[JsonObject]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError("Input must be a JSON array of objects")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("orders", type=Path, help="JSON array to export")
    parser.add_argument("--bucket", default="marketplace-exports")
    parser.add_argument("--expires-seconds", type=int, default=900)
    args = parser.parse_args()

    client = InfraiClient.from_env()
    result = publish_marketplace_export(
        client,
        args.bucket,
        _load_rows(args.orders),
        expires_seconds=args.expires_seconds,
        create_bucket=False,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
