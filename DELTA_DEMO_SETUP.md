# Delta Exchange Demo setup

The active bot configuration is set to `delta_demo` and restricted to `BTCUSD`.
The API client is pinned to Delta India Testnet and does not contain any key or
secret. The defaults keep Demo orders disabled; the active configuration has
automatic Demo orders enabled at the user's request. Do not paste credentials
into source files, this document, or chat.

## Before starting

1. Revoke the API key and secret that were shared in chat. Create a new key
   from the Delta Demo account (`demo.delta.exchange`), not the production
   account.
2. Enable only the required **Read Data** and **Trading** permissions. Add this
   PC's current public IP to the Demo key's IP whitelist. Do not enable
   withdrawals or transfers.
3. On Windows, open **Edit environment variables for your account** and add
   user variables named `DELTA_API_KEY` and `DELTA_API_SECRET`. Enter the new
   values there, then close and reopen the bot terminal so it inherits them.
4. Clear BTCUSD positions and open orders in the Demo account before starting.
   At startup, the bot blocks trading if it finds any existing exposure/order.
5. Start with `Start_Trading.bat`. The launcher sets public market data to the
   Demo/Testnet environment. Restart the engine after updating code or
   credentials. The Monitor page must show Delta Demo connected and ready before
   signal execution can proceed; the engine blocks entries until the position,
   open-order, and settlement-wallet checks all pass.

The first version converts the configured BTC quantity to whole contracts
using the live BTCUSD product value and refuses to trade if it is not exactly
representable. It confirms the exchange position and protective bracket before
recording the entry locally. Daily loss/profit guards use the Demo wallet
balance at the start of the local day plus current unrealized position P&L. If a
request times out or exchange state cannot be confirmed, automatic entries
stop and the Monitor page asks for manual review.
Order client IDs respect Delta's 32-character maximum.

The absolute daily-loss setting is interpreted in the contract settlement
asset shown by Delta (the dashboard labels this as settlement units in Demo
mode); the percent limit is based on that day's starting wallet balance.

Paper balance and closed-trade cards remain local simulator records. Delta
Demo position size, entry, and bracket information are shown in Open Positions
and Monitor. Exchange positions are managed by their exchange-side SL/TP; the
bot does not yet reconcile Delta fill history into its local closed-trade P&L.
