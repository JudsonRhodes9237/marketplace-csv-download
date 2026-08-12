import unittest
from unittest.mock import patch

from marketplace_export import _upload_signed_url, publish_marketplace_export, render_csv


class RecordingClient:
    def __init__(self) -> None:
        self.calls = []

    def storage_bucket_create(self, bucket):
        self.calls.append(("create", bucket))
        return {"bucket": bucket}

    def storage_object_presign(self, bucket, key, body):
        self.calls.append(("presign", bucket, key, body))
        return {"url": "https://signed.example/upload" if body["op"] == "put" else "https://signed.example/download"}


class MarketplaceExportTest(unittest.TestCase):
    def test_csv_uses_standard_quoting(self):
        content = render_csv([{"order_id": "ord_1", "product": "Tea, large", "quantity": 2}])
        self.assertEqual(
            content.decode("utf-8"),
            'order_id,product,quantity\nord_1,"Tea, large",2\n',
        )

    def test_publish_creates_bucket_uploads_and_returns_download(self):
        client = RecordingClient()
        uploaded = []

        result = publish_marketplace_export(
            client,
            "marketplace-exports",
            [{"order_id": "ord_1", "total": "24.00"}],
            upload=lambda signed, content: uploaded.append((signed, content)),
            expires_seconds=600,
        )

        self.assertEqual(client.calls[0], ("create", "marketplace-exports"))
        self.assertEqual(client.calls[1][3]["op"], "put")
        self.assertEqual(client.calls[2][3]["op"], "get")
        self.assertEqual(uploaded[0][0]["url"], "https://signed.example/upload")
        self.assertEqual(result["download_url"], "https://signed.example/download")
        self.assertEqual(result["expires_seconds"], 600)

    def test_post_upload_uses_signed_file_content_type(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        def open_request(request):
            captured["body"] = request.data
            return Response()

        signed = {
            "url": "https://signed.example/upload",
            "method": "POST",
            "fields": {"key": "exports/orders.csv", "Content-Type": "text/csv"},
        }
        with patch("marketplace_export.urlopen", side_effect=open_request):
            _upload_signed_url(signed, b"order_id\nord_1\n")

        self.assertIn(b"Content-Type: text/csv\r\n\r\n", captured["body"])
        self.assertNotIn(b"text/csv; charset=utf-8", captured["body"])


if __name__ == "__main__":
    unittest.main()
