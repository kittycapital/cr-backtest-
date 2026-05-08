"""
Liquity CR Signal Backtest — Data Collection
Fetches Liquity V1 TVL, LUSD supply, ETH price from DeFiLlama.
Calculates CR, CR Distance Oscillator, and detects local tops/bottoms.
"""

import json
import requests
from datetime import datetime, timezone

# ─── CONFIG ───────────────────────────────────────────────────────
WINDOW_DAYS = 30          # rolling window for local top/bottom detection
MIN_GAP_DAYS = 45         # minimum days between two consecutive tops/bottoms
RECOVERY_THRESHOLD = 150  # Liquity Recovery Mode threshold
OUTPUT_FILE = "data.json"


def fetch_json(url, label=""):
    print(f"  Fetching {label or url} ...")
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.json()


def day_key(ts):
    """Round unix timestamp to start-of-day."""
    return int(ts) // 86400 * 86400


def find_extremes(data, window):
    """Find local tops and bottoms using a rolling window on ethPrice."""
    tops, bottoms = [], []
    for i in range(window, len(data) - window):
        price = data[i]["ethPrice"]
        if price is None:
            continue
        is_top = all(
            data[j]["ethPrice"] is None or data[j]["ethPrice"] <= price
            for j in range(i - window, i + window + 1) if j != i
        )
        is_bottom = all(
            data[j]["ethPrice"] is None or data[j]["ethPrice"] >= price
            for j in range(i - window, i + window + 1) if j != i
        )
        if is_top:
            tops.append(data[i])
        if is_bottom:
            bottoms.append(data[i])
    return tops, bottoms


def dedup(arr, min_gap, prefer_higher=True):
    """Remove duplicates that are too close together, keeping the more extreme."""
    if not arr:
        return arr
    arr.sort(key=lambda x: x["ts"])
    result = [arr[0]]
    for item in arr[1:]:
        gap = (item["ts"] - result[-1]["ts"]) / 86400
        if gap >= min_gap:
            result.append(item)
        else:
            last = result[-1]
            if prefer_higher and item["ethPrice"] > last["ethPrice"]:
                result[-1] = item
            elif not prefer_higher and item["ethPrice"] < last["ethPrice"]:
                result[-1] = item
    return result


def main():
    print("=" * 60)
    print("Liquity CR Signal — Data Collection")
    print("=" * 60)

    # 1) Stablecoins list → find LUSD id
    stables = fetch_json("https://stablecoins.llama.fi/stablecoins", "stablecoins list")
    lusd = next(
        (s for s in stables.get("peggedAssets", [])
         if s.get("symbol") == "LUSD" or "liquity" in s.get("name", "").lower()),
        None
    )
    if not lusd:
        raise RuntimeError("LUSD not found in DeFiLlama stablecoins")
    lusd_id = lusd["id"]
    print(f"  → LUSD id = {lusd_id}")

    # 2) LUSD supply history
    lusd_data = fetch_json(
        f"https://stablecoins.llama.fi/stablecoin/{lusd_id}", "LUSD supply"
    )
    lusd_hist = {}
    for t in lusd_data.get("tokens", []):
        dk = day_key(t["date"])
        circ = (t.get("circulating") or {}).get("peggedUSD", 0)
        if circ > 0:
            lusd_hist[dk] = circ

    # 3) Liquity V1 TVL
    tvl_data = fetch_json(
        "https://api.llama.fi/protocol/liquity-v1", "Liquity V1 TVL"
    )
    tvl_hist = {}
    for t in tvl_data.get("tvl", []):
        dk = day_key(t["date"])
        tvl_hist[dk] = t["totalLiquidityUSD"]

    # 4) ETH price — read from local CSV + supplement recent from CoinGecko
    eth_hist = {}
    import csv
    ETH_CSV = "ethereum.csv"
    print(f"  Reading {ETH_CSV} ...")
    with open(ETH_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # format: "2021-04-05 00:00:00 UTC"
            dt = datetime.strptime(row["snapped_at"][:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            dk = day_key(int(dt.timestamp()))
            price = float(row["price"])
            if price > 0:
                eth_hist[dk] = price
    print(f"  → {len(eth_hist)} ETH price points from CSV")

    # Supplement: fill dates after CSV with CoinGecko API
    max_csv_ts = max(eth_hist.keys()) if eth_hist else 0
    today_ts = day_key(int(datetime.now(timezone.utc).timestamp()))
    gap_days = (today_ts - max_csv_ts) // 86400
    if gap_days > 1:
        print(f"  CSV is {gap_days} days behind, fetching recent from CoinGecko...")
        try:
            cg_url = f"https://api.coingecko.com/api/v3/coins/ethereum/market_chart?vs_currency=usd&days={gap_days + 5}&interval=daily"
            cg_data = fetch_json(cg_url, "ETH recent (CoinGecko)")
            added = 0
            for ts_ms, price in cg_data.get("prices", []):
                dk = day_key(ts_ms / 1000)
                if dk > max_csv_ts and price > 0:
                    eth_hist[dk] = price
                    added += 1
            print(f"  → +{added} recent points from CoinGecko")
        except Exception as e:
            print(f"  CoinGecko supplement failed (non-critical): {e}")

    print(f"  → {len(eth_hist)} total ETH price points")

    # 5) Merge & calculate CR
    print("\n  Merging data and calculating CR ...")
    all_dates = sorted(set(list(tvl_hist.keys()) + list(lusd_hist.keys())))
    last_tvl, last_lusd, last_eth = 0, 0, 0
    merged = []

    for ts in all_dates:
        tvl = tvl_hist.get(ts, last_tvl)
        lusd_supply = lusd_hist.get(ts, last_lusd)
        eth_price = eth_hist.get(ts, last_eth)
        last_tvl = tvl
        last_lusd = lusd_supply
        if eth_price:
            last_eth = eth_price

        if lusd_supply > 0 and tvl > 0 and eth_price:
            cr = (tvl / lusd_supply) * 100
            merged.append({
                "ts": ts,
                "date": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
                "ethPrice": round(eth_price, 2),
                "tvl": round(tvl, 0),
                "lusdSupply": round(lusd_supply, 0),
                "cr": round(min(cr, 800), 2),
                "crDistance": round(min(cr, 800) - RECOVERY_THRESHOLD, 2),
            })

    print(f"  → {len(merged)} data points ({merged[0]['date']} ~ {merged[-1]['date']})")

    # 6) Detect tops & bottoms
    print(f"\n  Detecting local tops/bottoms (window={WINDOW_DAYS}d, gap={MIN_GAP_DAYS}d) ...")
    raw_tops, raw_bottoms = find_extremes(merged, WINDOW_DAYS)
    tops = dedup(raw_tops, MIN_GAP_DAYS, prefer_higher=True)
    bottoms = dedup(raw_bottoms, MIN_GAP_DAYS, prefer_higher=False)

    top_crs = [t["cr"] for t in tops]
    bottom_crs = [b["cr"] for b in bottoms]
    avg_top_cr = sum(top_crs) / len(top_crs) if top_crs else 0
    avg_bottom_cr = sum(bottom_crs) / len(bottom_crs) if bottom_crs else 0

    # 7) Recovery mode entries
    recovery_entries = []
    for i in range(1, len(merged)):
        if merged[i - 1]["cr"] >= RECOVERY_THRESHOLD and merged[i]["cr"] < RECOVERY_THRESHOLD:
            recovery_entries.append(merged[i])

    # 8) Build output
    output = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "config": {
            "windowDays": WINDOW_DAYS,
            "minGapDays": MIN_GAP_DAYS,
            "recoveryThreshold": RECOVERY_THRESHOLD,
        },
        "stats": {
            "dataPoints": len(merged),
            "dateRange": f"{merged[0]['date']} ~ {merged[-1]['date']}",
            "topCount": len(tops),
            "bottomCount": len(bottoms),
            "avgTopCR": round(avg_top_cr, 1),
            "avgBottomCR": round(avg_bottom_cr, 1),
            "avgTopDistance": round(avg_top_cr - RECOVERY_THRESHOLD, 1),
            "avgBottomDistance": round(avg_bottom_cr - RECOVERY_THRESHOLD, 1),
            "minCR": round(min(m["cr"] for m in merged), 1),
            "maxCR": round(max(m["cr"] for m in merged), 1),
            "recoveryCount": len(recovery_entries),
        },
        "chart": merged,
        "tops": tops,
        "bottoms": bottoms,
        "recoveryEntries": recovery_entries,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False)

    print(f"\n  ✅ Saved to {OUTPUT_FILE} ({len(merged)} points)")
    print(f"  Tops: {len(tops)} (avg CR {avg_top_cr:.1f}%)")
    print(f"  Bottoms: {len(bottoms)} (avg CR {avg_bottom_cr:.1f}%)")
    print(f"  Recovery entries: {len(recovery_entries)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
