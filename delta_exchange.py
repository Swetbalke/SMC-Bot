"""Delta Exchange India Demo API client.

This client is deliberately pinned to the India testnet host. It never falls
back to production, and it only reads credentials from process environment.

Timestamp sync: local clock drift causes expired_signature (HTTP 401).
_get_server_time() fetches Delta's /v2/time and computes a drift offset so
every authenticated request uses server-aligned time, not local time.
"""
import hashlib
import hmac
import json
import os
import time
import threading
from urllib.parse import urlencode

import requests


BASE_URL = "https://cdn-ind.testnet.deltaex.org"

# How often to re-sync the clock offset (seconds).
_SYNC_INTERVAL = 300   # 5 minutes


class DeltaAPIError(RuntimeError):
    pass


class DeltaDemoClient:
    def __init__(self, api_key=None, api_secret=None, timeout=10):
        self.api_key = api_key or os.environ.get("DELTA_API_KEY", "").strip()
        self.api_secret = api_secret or os.environ.get("DELTA_API_SECRET", "").strip()
        self.timeout = timeout
        # Clock-drift correction: server_time - local_time (seconds, may be negative)
        self._time_offset: float = 0.0
        self._offset_lock = threading.Lock()
        self._last_sync: float = 0.0
        if not self.api_key or not self.api_secret:
            raise DeltaAPIError("Demo credentials missing. Set DELTA_API_KEY and DELTA_API_SECRET locally.")
        # Sync immediately on construction so the very first request is accurate.
        self._sync_time_offset()

    # ------------------------------------------------------------------ #
    # Clock-sync helpers
    # ------------------------------------------------------------------ #

    def _sync_time_offset(self) -> None:
        """
        Fetch Delta's server timestamp and compute local drift.
        Uses /v2/time (unauthenticated) so no credentials are required.
        Silently ignores failures — drift stays at whatever the last good
        value was (or 0 on first call if the server is unreachable).
        """
        t_before = time.time()
        try:
            resp = requests.get(
                BASE_URL + "/v2/time",
                headers={"Accept": "application/json", "User-Agent": "tradingbot-demo/1.0"},
                timeout=5,
            )
            t_after = time.time()
            data = resp.json()
            # Delta returns {"result": {"server_time": <unix_ms>}, "success": true}
            result = data.get("result") or {}
            server_ms = result.get("server_time") or result.get("timestamp")
            if server_ms is None:
                # Some versions return the value directly
                server_ms = data.get("server_time") or data.get("timestamp")
            if server_ms is None:
                return
            server_sec = int(server_ms) / 1000.0 if int(server_ms) > 1_000_000_000_000 else float(server_ms)
            # Round-trip mid-point estimate of local time
            local_mid = (t_before + t_after) / 2.0
            offset = server_sec - local_mid
            with self._offset_lock:
                self._time_offset = offset
                self._last_sync = t_after
        except Exception:
            # Network error or unexpected format — keep existing offset
            pass

    def _server_timestamp(self) -> str:
        """
        Return a Unix-second timestamp aligned to Delta's server clock.
        Re-syncs the offset every _SYNC_INTERVAL seconds.
        Falls back gracefully if the sync endpoint is unavailable.
        """
        with self._offset_lock:
            age = time.time() - self._last_sync
        if age > _SYNC_INTERVAL:
            self._sync_time_offset()
        with self._offset_lock:
            offset = self._time_offset
        return str(int(time.time() + offset))

    # ------------------------------------------------------------------ #
    def _request_with_retry(self, method, path, params=None, body=None,
                            authenticated=True, max_retries=3):
        """Retry safe reads on transient failures; never replay order submissions."""
        method = method.upper()
        if method != "GET":
            return self._request(method, path, params, body, authenticated)
        delay = 1.0
        clock_retry_used = False
        for attempt in range(max_retries):
            try:
                return self._request(method, path, params, body, authenticated)
            except DeltaAPIError as exc:
                message = str(exc).lower()
                if "expired_signature" in message and not clock_retry_used:
                    # _request refreshes the exchange clock before raising this error.
                    # Replay only this idempotent read once with the refreshed timestamp.
                    clock_retry_used = True
                    continue
                transient = ("request failed" in message or "http 429" in message
                             or any(f"http {code}" in message for code in range(500, 600)))
                if not transient or attempt + 1 >= max_retries:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 30)

    # Core request
    # ------------------------------------------------------------------ #

    def _request(self, method, path, params=None, body=None, authenticated=True):
        method = method.upper()
        # Sign the exact query-string ordering that requests will transmit.
        # A different ordering makes an otherwise valid HMAC fail verification.
        ordered_params = dict(sorted((params or {}).items()))
        query = urlencode(ordered_params)
        payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body is not None else ""
        headers = {"Accept": "application/json", "User-Agent": "tradingbot-demo/1.0"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if authenticated:
            timestamp = self._server_timestamp()   # ← server-aligned, not local
            prehash = method + timestamp + path + (("?" + query) if query else "") + payload
            signature = hmac.new(self.api_secret.encode(), prehash.encode(), hashlib.sha256).hexdigest()
            headers.update({"api-key": self.api_key, "timestamp": timestamp, "signature": signature})
        try:
            response = requests.request(method, BASE_URL + path, params=ordered_params, data=payload or None,
                                        headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise DeltaAPIError(f"Delta testnet request failed: {type(exc).__name__}") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise DeltaAPIError(f"Delta testnet returned HTTP {response.status_code} without JSON") from exc
        if not response.ok or not data.get("success", False):
            # Avoid including headers or request values; only safe API error text is surfaced.
            err = data.get("error", {})
            code = err.get("code", "request_failed") if isinstance(err, dict) else "request_failed"
            if code == "ip_not_whitelisted_for_api_key":
                raise DeltaAPIError("This PC's public IP is not whitelisted for the Demo API key. Add it under Delta Account > Manage API Keys.")
            if code == "expired_signature":
                # Force an immediate re-sync and surface a clear message.
                self._sync_time_offset()
                with self._offset_lock:
                    drift = self._time_offset
                raise DeltaAPIError(
                    f"Delta testnet expired_signature (HTTP 401) — "
                    f"clock drift was {drift:+.1f}s; offset refreshed. "
                    f"If this repeats, sync your PC clock (Windows Time / NTP)."
                )
            raise DeltaAPIError(f"Delta testnet {code} (HTTP {response.status_code})")
        return data.get("result")

    def get_product(self, symbol="BTCUSD"):
        if symbol != "BTCUSD":
            raise DeltaAPIError("Only BTCUSD is enabled for the initial demo integration")
        return self._request_with_retry("GET", f"/v2/products/{symbol}", authenticated=False)

    def get_server_time(self):
        """Read Delta's server-time endpoint without sending credentials."""
        return self._request_with_retry("GET", "/v2/time", authenticated=False)

    def get_position(self, product_id):
        return self._request_with_retry("GET", "/v2/positions", params={"product_id": int(product_id)})

    def get_wallet_balances(self):
        result = self._request_with_retry("GET", "/v2/wallet/balances")
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ("wallets", "balances", "result"):
                value = result.get(key)
                if isinstance(value, list):
                    return value
            values = list(result.values())
            if values and all(isinstance(value, dict) for value in values):
                return values
        return []

    def get_open_orders(self, product_id):
        active = []
        for state in ("open", "pending"):
            result = self._request_with_retry("GET", "/v2/orders",
                                   params={"product_id": int(product_id), "state": state})
            if isinstance(result, list):
                active.extend(result)
            elif result:
                active.append(result)
        return active

    def get_order_by_client_order_id(self, client_order_id):
        """Read an order after an ambiguous submission without creating another."""
        if not client_order_id or len(str(client_order_id)) > 32:
            raise DeltaAPIError("client_order_id must be 1-32 characters")
        return self._request_with_retry(
            "GET", f"/v2/orders/client_order_id/{client_order_id}")

    def place_market_order(self, product_id, symbol, direction, contracts, client_order_id):
        if direction not in {"LONG", "SHORT"} or int(contracts) != contracts or contracts <= 0:
            raise DeltaAPIError("Order side or integer contract quantity is invalid")
        if not client_order_id or len(str(client_order_id)) > 32:
            raise DeltaAPIError("client_order_id must be 1-32 characters")
        body = {"product_id": int(product_id), "size": int(contracts),
                "side": "buy" if direction == "LONG" else "sell",
                "order_type": "market_order", "client_order_id": str(client_order_id)}
        return self._request("POST", "/v2/orders", body=body)

    def close_position_market(self, product_id, contracts, direction, client_order_id):
        if not client_order_id or len(str(client_order_id)) > 32:
            raise DeltaAPIError("client_order_id must be 1-32 characters")
        body = {"product_id": int(product_id), "size": int(contracts),
                "side": "sell" if direction == "LONG" else "buy",
                "order_type": "market_order", "reduce_only": True,
                "client_order_id": str(client_order_id)}
        return self._request("POST", "/v2/orders", body=body)

    def place_bracket(self, product_id, symbol, stop_loss, take_profit=None):
        sl = {"order_type": "market_order", "stop_price": str(stop_loss)}
        body = {"product_id": int(product_id), "stop_loss_order": sl,
                "bracket_stop_trigger_method": "mark_price"}
        if take_profit is not None and float(take_profit) > 0:
            body["take_profit_order"] = {"order_type": "market_order", "stop_price": str(take_profit)}
        return self._request("POST", "/v2/orders/bracket", body=body)

    def update_bracket(self, order_id, product_id, stop_loss, take_profit=None):
        body = {"id": int(order_id), "product_id": int(product_id),
                "bracket_stop_loss_price": str(stop_loss),
                "bracket_stop_trigger_method": "mark_price"}
        if take_profit is not None and float(take_profit) > 0:
            body["bracket_take_profit_price"] = str(take_profit)
        return self._request("PUT", "/v2/orders/bracket", body=body)

    def get_status(self, symbol="BTCUSD"):
        product = self.get_product(symbol)
        pid = product.get("id")
        position = self.get_position(pid)
        orders = self.get_open_orders(pid)
        return {"environment": "Delta India Demo/Testnet", "connected": True,
                "symbol": symbol, "product_id": pid, "position": position,
                "open_orders": orders, "checked_at": int(time.time())}
