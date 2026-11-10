import hashlib
import hmac
import unittest
from unittest.mock import patch

from delta_exchange import DeltaDemoClient


class DeltaClientTests(unittest.TestCase):
    def make_client(self):
        with patch.object(DeltaDemoClient, "_sync_time_offset"):
            return DeltaDemoClient(api_key="test-key", api_secret="test-secret")

    def test_request_signature_uses_canonical_sorted_query(self):
        client = self.make_client()
        expected_timestamp = "123"
        expected_prehash = "GET123/v2/orders?product_id=1&state=open"
        expected_signature = hmac.new(
            b"test-secret", expected_prehash.encode(), hashlib.sha256).hexdigest()

        class Response:
            ok = True
            status_code = 200

            @staticmethod
            def json():
                return {"success": True, "result": []}

        with patch.object(client, "_server_timestamp", return_value=expected_timestamp), \
                patch("delta_exchange.requests.request", return_value=Response()) as request:
            client._request("GET", "/v2/orders", {"state": "open", "product_id": 1})

        headers = request.call_args.kwargs["headers"]
        self.assertEqual(headers["signature"], expected_signature)
        self.assertEqual(list(request.call_args.kwargs["params"].items()),
                         [("product_id", 1), ("state", "open")])

    def test_client_order_lookup_uses_read_only_endpoint(self):
        client = self.make_client()
        with patch.object(client, "_request_with_retry", return_value={"id": 42}) as request:
            result = client.get_order_by_client_order_id("abc123")
        self.assertEqual(result["id"], 42)
        request.assert_called_once_with("GET", "/v2/orders/client_order_id/abc123")

    def test_server_time_is_an_unauthenticated_read(self):
        client = self.make_client()
        with patch.object(client, "_request_with_retry", return_value={"server_time": 123}) as request:
            client.get_server_time()
        request.assert_called_once_with("GET", "/v2/time", authenticated=False)


if __name__ == "__main__":
    unittest.main()
