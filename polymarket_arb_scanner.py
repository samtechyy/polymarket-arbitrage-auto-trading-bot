import os
import time
import json
import logging
from typing import List, Dict, Any, Optional

import requests
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY


# -------- CONFIG --------

# Polymarket Gamma markets endpoint (read-only)
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"

# CLOB trading endpoint
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon

# Polling / arb parameters
POLL_INTERVAL_SECONDS = 30

FEE_BUFFER = 0.03      # 3% buffer for fees + slippage
MIN_EDGE = 0.01        # 1% minimum edge to trigger trades
TARGET_PAYOUT_PER_MARKET_USD = 10.0  # Target guaranteed payout size per market (approx)

# Category / liquidity filters
TARGET_CATEGORIES = {"Sports", "Crypto", "Politics"}
MIN_LIQUIDITY = 5_000  # Set to 0 to disable

# Safety: start with DRY_RUN = True (no real orders)
DRY_RUN = True

# Do not trade same market repeatedly in one session
EXECUTED_MARKETS: set[str] = set()


# -------- LOGGING --------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# -------- ENV & CLIENT SETUP --------

def load_env_and_init_client() -> Optional[ClobClient]:
    """
    Load secrets from .env and create an authenticated ClobClient.
    If secrets are missing, returns None and we stay in dry-run mode.
    """
    load_dotenv()

    private_key = os.getenv("POLY_PRIVATE_KEY")
    funder = os.getenv("POLY_FUNDER_ADDRESS")
    sig_type_str = os.getenv("POLY_SIGNATURE_TYPE", "0")

    try:
        signature_type = int(sig_type_str)
    except ValueError:
        signature_type = 0

    if not private_key or not funder:
        logging.warning("Missing POLY_PRIVATE_KEY or POLY_FUNDER_ADDRESS - staying in DRY_RUN mode.")
        return None

    client = ClobClient(
        CLOB_HOST,
        key=private_key,
        chain_id=CHAIN_ID,
        signature_type=signature_type,
        funder=funder,
    )
    client.set_api_creds(client.create_or_derive_api_creds())

    ok = client.get_ok()
    logging.info("Connected to CLOB: ok=%s", ok)

    return client


# -------- FILTERS --------

def is_target_category(market: dict) -> bool:
    """Filter to sports / crypto / politics style markets."""
    cat = (market.get("category") or "").strip()
    subcat = (market.get("subcategory") or "").strip()

    cat_norm = cat.lower()
    subcat_norm = subcat.lower()

    if cat_norm in {"sports", "crypto", "politics"}:
        return True

    if any(x in subcat_norm for x in ["sports", "nba", "nfl", "soccer", "mlb"]):
        return True
    if any(x in subcat_norm for x in ["crypto", "bitcoin", "btc", "eth", "solana"]):
        return True
    if any(x in subcat_norm for x in ["politics", "election", "primary", "senate", "president"]):
        return True

    return False


def has_enough_liquidity(market: dict) -> bool:
    """Filter low-liquidity markets out."""
    liq = market.get("liquidity") or market.get("liquidityNum") or 0
    try:
        liq = float(liq)
    except (TypeError, ValueError):
        return False
    return liq >= MIN_LIQUIDITY


# -------- GAMMA MARKETS FETCH --------

def fetch_markets() -> List[Dict[str, Any]]:
    """
    Fetch markets from Polymarket's Gamma API.
    """
    params = {
        "active": "true",
        "resolved": "false",
    }
    resp = requests.get(GAMMA_MARKETS_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if isinstance(data, dict) and "markets" in data:
        return data["markets"]
    if isinstance(data, list):
        return data
    raise ValueError("Unexpected markets response format")


# -------- OUTCOME / TOKEN MAPPING --------

def extract_outcomes_with_tokens(market: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extract outcomes, prices, and CLOB token IDs from Gamma markets.

    Gamma markets often encode these as JSON strings:
      - outcomes:       '["YES","NO"]'
      - outcomePrices:  '["0.48","0.55"]'
      - clobTokenIds:   '["0x...","0x..."]'

    Return:
    [
      {"name": "YES", "price": 0.48, "token_id": "0x..."},
      {"name": "NO",  "price": 0.55, "token_id": "0x..."},
      ...
    ]
    """
    outcomes: List[Dict[str, Any]] = []

    try:
        names_raw = market.get("outcomes") or "[]"
        prices_raw = market.get("outcomePrices") or "[]"
        tokens_raw = market.get("clobTokenIds") or "[]"

        names = json.loads(names_raw) if isinstance(names_raw, str) else names_raw
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        token_ids = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
    except Exception as e:
        logging.debug("Failed to parse outcomes for market %s: %s", market.get("id"), e)
        return []

    for name, price, token_id in zip(names, prices, token_ids):
        try:
            p = float(price)
        except (TypeError, ValueError):
            continue

        if not (0.0 < p < 1.0):
            continue

        if not token_id:
            continue

        outcomes.append(
            {
                "name": str(name),
                "price": p,
                "token_id": str(token_id),
            }
        )

    return outcomes


# -------- ARBITRAGE CHECK --------

def summarize_market(market: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": market.get("id"),
        "slug": market.get("slug"),
        "question": market.get("question") or market.get("title"),
        "category": market.get("category"),
        "subcategory": market.get("subcategory"),
        "url": f"https://polymarket.com/market/{market.get('slug')}" if market.get("slug") else None,
        "liquidity": market.get("liquidity") or market.get("liquidityNum"),
        "volume24h": market.get("volume24hr"),
    }


def analyze_market_for_arb(market: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Simple long-basket arb:
    - If sum(outcome prices) < 1 - FEE_BUFFER - MIN_EDGE
      we consider buying every outcome once.
    """
    outcomes = extract_outcomes_with_tokens(market)
    if len(outcomes) < 2:
        return None

    sum_prices = sum(o["price"] for o in outcomes)

    long_threshold = 1.0 - FEE_BUFFER - MIN_EDGE

    if sum_prices < long_threshold:
        edge = (1.0 - FEE_BUFFER) - sum_prices
        return {
            "type": "long_basket",
            "edge": edge,
            "sum_prices": sum_prices,
            "market": summarize_market(market),
            "outcomes": outcomes,
        }

    return None


def find_arbitrage_opportunities(markets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    opportunities: List[Dict[str, Any]] = []

    for m in markets:
        if not is_target_category(m):
            continue
        if MIN_LIQUIDITY > 0 and not has_enough_liquidity(m):
            continue

        try:
            opp = analyze_market_for_arb(m)
        except Exception as e:
            logging.debug("Error analyzing market %s: %s", m.get("id"), e)
            continue

        if opp is not None:
            opportunities.append(opp)

    return opportunities


# -------- EXECUTION --------

def print_opportunity(opp: Dict[str, Any]) -> None:
    m = opp["market"]
    logging.info(
        "ARB FOUND [%s] edge=%.2f%% sum=%.4f type=%s",
        m.get("slug") or m.get("id"),
        opp["edge"] * 100,
        opp["sum_prices"],
        opp["type"],
    )
    logging.info("  Question: %s", m.get("question"))
    if m.get("category"):
        logging.info("  Category: %s / %s", m.get("category"), m.get("subcategory"))
    if m.get("url"):
        logging.info("  URL: %s", m["url"])
    if m.get("liquidity") is not None:
        logging.info("  Liquidity: %s", m["liquidity"])
    logging.info("  Outcomes:")
    for o in opp["outcomes"]:
        logging.info("    - %s: price=%.4f token_id=%s", o["name"], o["price"], o["token_id"])
    logging.info("-" * 80)


def execute_long_basket_arb(client: Optional[ClobClient], opp: Dict[str, Any]) -> None:
    """
    Execute a simple long-basket: buy each outcome with equal USD notional.

    This is extremely simplified and does not manage inventory / hedging.
    """
    m = opp["market"]
    slug = m.get("slug") or m.get("id") or ""
    if slug in EXECUTED_MARKETS:
        logging.info("Already traded this market in this session (%s); skipping.", slug)
        return

    print_opportunity(opp)

    num_outcomes = len(opp["outcomes"])
    if num_outcomes == 0:
        return

    usd_per_outcome = TARGET_PAYOUT_PER_MARKET_USD / num_outcomes

    logging.info(
        "Planning to buy each outcome for ~%.2f USD (DRY_RUN=%s)",
        usd_per_outcome,
        DRY_RUN,
    )

    if client is None or DRY_RUN:
        logging.info("DRY_RUN or no client: not sending real orders; just logging.")
        EXECUTED_MARKETS.add(slug)
        return

    for o in opp["outcomes"]:
        token_id = o["token_id"]

        try:
            mo = MarketOrderArgs(
                token_id=token_id,
                amount=usd_per_outcome,
                side=BUY,
                order_type=OrderType.FOK,  # Fill-or-kill to avoid partial weirdness
            )
            signed = client.create_market_order(mo)
            resp = client.post_order(signed, OrderType.FOK)
            logging.info("Order placed token=%s amount=%.2f resp=%s", token_id, usd_per_outcome, resp)
        except Exception as e:
            logging.error("Error placing order for token %s: %s", token_id, e)

    EXECUTED_MARKETS.add(slug)


# -------- MAIN LOOP --------

def main() -> None:
    logging.info("Starting Polymarket arbitrage auto-trader")
    logging.info(
        "Categories: %s | MIN_LIQUIDITY=%s | FEE_BUFFER=%.2f | MIN_EDGE=%.2f | DRY_RUN=%s",
        ", ".join(TARGET_CATEGORIES),
        MIN_LIQUIDITY,
        FEE_BUFFER,
        MIN_EDGE,
        DRY_RUN,
    )

    client = load_env_and_init_client()
    if client is None:
        logging.warning("Trading client not initialized; will run in DRY_RUN mode only.")

    while True:
        try:
            markets = fetch_markets()
            logging.info("Fetched %d markets", len(markets))

            opps = find_arbitrage_opportunities(markets)
            if opps:
                logging.info("Found %d potential opportunities", len(opps))
                for opp in opps:
                    if opp["type"] == "long_basket":
                        execute_long_basket_arb(client, opp)
            else:
                logging.info("No clear arbitrage opportunities above %.2f%% edge", MIN_EDGE * 100)

        except Exception as e:
            logging.error("Error in main loop: %s", e)

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()