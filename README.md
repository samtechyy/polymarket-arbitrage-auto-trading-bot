## Polymarket Arbitrage Scanner & Auto‑Trader

This project contains Python scripts to:

- **Scan Polymarket** for simple arbitrage opportunities in **sports**, **crypto**, and **politics** markets using the public Gamma markets API.
- **auto‑trade** those opportunities using Polymarket’s **CLOB** (Central Limit Order Book) via the official `py-clob-client`. You can choose to use it only for scanning.

> **Important**: This is experimental software. It can lose money due to bugs, fees, slippage, bad assumptions, or API changes. Use at your own risk. Nothing here is financial, legal, or tax advice.

---

### Files

- **`polymarket_arb_scanner.py`**  
  Read‑only scanner. Fetches markets and prints potential arbitrage opportunities.

- **`polymarket_arb_autotrader.py`**  
  Scanner + (optional) auto‑trader. Can place orders on Polymarket via the CLOB when configured.

- **`.env`** (you create this)  
  Holds your private key and wallet address for trading. **Never commit or share this.**

---

### Requirements

- **Python** 3.9 or newer (3.10+ recommended)
- A Polygon wallet with funds (USDC + Polymarket conditional tokens as required)
- Basic familiarity with the command line

Install dependencies:

```bash
pip install requests py-clob-client python-dotenv
```

---

### Environment configuration (`.env`)

Create a file named `.env` in the same folder as the scripts:

```text
POLY_PRIVATE_KEY=0xYOUR_PRIVATE_KEY_HERE
POLY_FUNDER_ADDRESS=0xYOUR_WALLET_ADDRESS_HERE
POLY_SIGNATURE_TYPE=0
```

- **`POLY_PRIVATE_KEY`**: Your Polygon wallet private key (same one you’d import to MetaMask).  
  - Keep this secret. Do **not** commit it to git.
- **`POLY_FUNDER_ADDRESS`**: The public address (`0x...`) of the wallet that actually holds your funds on Polymarket.
- **`POLY_SIGNATURE_TYPE`**:
  - `0` – Standard EOA wallet (MetaMask / hardware, direct private key)
  - `1` – Email / Magic wallet signatures
  - `2` – Browser wallet proxy signatures

If `POLY_PRIVATE_KEY` or `POLY_FUNDER_ADDRESS` are missing, the auto‑trader script will stay in **dry‑run mode** and not place real trades.

---

### Running the read‑only scanner

The basic scanner script only **prints** opportunities and never trades.

```bash
python polymarket_arb_scanner.py
```

It will:

- Pull active, unresolved markets from Polymarket’s Gamma API.
- Filter to **sports**, **crypto**, and **politics** categories.
- Apply a liquidity filter.
- Compute simple basket sums of outcome prices.
- Log any markets where the sum of prices indicates a clear arb after buffers.

You can tweak:

- `FEE_BUFFER` – approximate combined fees + slippage buffer (e.g. `0.03` = 3%)
- `MIN_EDGE` – minimum theoretical edge to bother logging (e.g. `0.01` = 1%)

---

### Running the auto‑trader (with dry‑run first)

The auto‑trader adds CLOB trading on top of the scanner logic.

1. Open the script and make sure:
   - `DRY_RUN = True` (start in dry‑run)
   - `TARGET_PAYOUT_PER_MARKET_USD` is small (e.g. `10.0` or less)
2. (Optional but recommended) Set up `.env` so the script can connect to the CLOB, even in dry‑run.
3. Run:

```bash
python polymarket_arb_autotrader.py
```

In **dry‑run mode**, it will:

- Discover arbitrage opportunities.
- Log what it *would* trade (token IDs, approximate USD per outcome).
- Not send any real orders.

Only **after** you’re satisfied with the behavior:

1. Make sure your wallet has **small** test funds.
2. Ensure Polymarket token allowances are set for USDC and conditional tokens (see Polymarket docs).
3. Change in the script:

```python
DRY_RUN = False
```

4. Optionally increase `TARGET_PAYOUT_PER_MARKET_USD` very slowly.

---

### How the trading logic works (high‑level)

- The script pulls Gamma markets and parses:
  - Outcome names
  - Outcome prices
  - `clobTokenIds` (CLOB token IDs used for trading)
- For each qualifying market:
  - If the **sum of outcome prices** \< \(1 - \text{FEE\_BUFFER} - \text{MIN\_EDGE}\), it flags a **long‑basket arbitrage**.
  - In auto‑trader mode, it allocates roughly `TARGET_PAYOUT_PER_MARKET_USD / number_of_outcomes` of USD to **buy each outcome** via `py-clob-client` market orders.
  - Uses **FOK (fill‑or‑kill)** market orders to avoid partial fills where possible.
- A simple in‑memory set tracks markets already traded in this session to avoid repeating the same trade too often.

This is a deliberately simple strategy; it does **not** manage inventory, closing positions, or complex risk.

---

### Safety & risk notes

- **Jurisdiction & KYC**: Ensure you are allowed to use Polymarket in your country and meet all legal requirements.
- **Start tiny**: Use very small `TARGET_PAYOUT_PER_MARKET_USD` and small wallet balances at first.
- **Expect API changes**: Polymarket’s APIs can change, breaking parsing or trading logic. Monitor logs.
- **Slippage & fees**: Even if an arb looks good on paper, fees, spreads, and thin books can remove the edge.
- **No guarantees**: This code is provided “as‑is” with no guarantees of correctness or profitability.

---

### Customization ideas

- Add **position tracking** and logic to unwind positions before resolution.
- Add **per‑day / per‑market exposure limits** (max USD per day, max open risk).
- Add logging to a file or database for backtesting and analysis.
- Extend arbitrage logic to handle **short‑basket** or cross‑market opportunities (more complex).

