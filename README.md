# amazon-cli

Search Amazon and check prices right from your terminal — sorted by rating,
filtered to popular brands with 1000+ reviews, with live prices, review counts,
and delivery dates.

> **Disclaimer:** Not affiliated with, authorized, or endorsed by Amazon.com, Inc.
> "Amazon" is a trademark of Amazon.com, Inc. This is an independent, personal,
> educational tool. You are responsible for complying with Amazon's Terms of
> Service. Use at your own risk, for low-volume personal use only.

## Usage

```bash
amazon-cli "4TB 2.5 SATA Internal SSD"             # top 10, best rated (default)
amazon-cli "4TB 2.5 SATA Internal SSD" --rating    # sorted by rating (high to low)
amazon-cli "4TB 2.5 SATA Internal SSD" --price     # sorted by price (low to high)
amazon-cli "4TB 2.5 SATA Internal SSD" --delivery  # sorted by delivery (fastest first)
amazon-cli "4TB 2.5 SATA Internal SSD" -n 20       # more results (default 10)
amazon-cli                                         # show examples
amazon-cli -h                                      # all options
```

### Color highlights

For each metric the **top two** results are color-coded — the best in **bold**,
the second in regular — so the winners jump out; everything else is plain white:

| Metric | Highlighted | Color |
|--------|-------------|-------|
| Price | two lowest | **green** |
| Rating | two highest | **cyan** |
| Reviews | two most-reviewed | **blue** |
| Delivery | two fastest (soonest arriving) | **yellow** |

So at a glance you can spot the cheapest, best-rated, most-reviewed, and
quickest-to-arrive — even without changing the sort.

The brand is shown with its **home country** in parentheses (e.g. `SAMSUNG (South
Korea)`) — the brand/company's country, **not** where the unit was manufactured.
Edit `_BRAND_COUNTRY` in `amazon-cli.py`.

## Default behavior

Every search shows the **top 10** results that are:
- **Recognized brands** only (Samsung, Crucial, WD, Seagate, TEAMGROUP, …);
  edit `_KNOWN_BRANDS` in `amazon-cli.py`
- **1000+ reviews** (so the ranking is stable and trustworthy)
- **Prime-eligible** — uses Amazon's own Prime filter, so foreign imports and
  non-Prime third-party offers are excluded (reliable, unlike scraping per item)
- **2.5" SATA type** — off-type items (M.2, NVMe, external) are filtered out
- **Sorted by rating**, high to low (override with `--price` or `--delivery`)

## Setup

Requires Python 3. Optionally install `curl_cffi` for browser impersonation
(reduces bot-detection blocks); the tool falls back to the standard library if
it's absent, so it always runs.

```bash
pip install curl_cffi        # optional but recommended
ln -s "$PWD/amazon-cli.py" ~/bin/amazon-cli   # so you can run it as `amazon-cli`
```

Optional environment variables:
- `AMAZON_TLD=co.uk` — use a different marketplace (default `com`); e.g. `AMAZON_TLD=co.uk amazon-cli "ssd"`

## How it works

1. **Fetch** the search page (via `curl_cffi` Chrome impersonation when available,
   else `urllib`). Responses are cached to disk for 10 minutes, so repeated
   searches make zero network requests.
2. **Parse** each result block with regex → brand, title, price, rating, count,
   delivery, Prime.
3. **Enrich** the top candidates by fetching their product pages in parallel for
   authoritative review counts, the model number, and delivery.
4. **Filter, sort, and display** the top N.

## Caveats

- **Runs from your own machine's IP.** Occasional requests from a home connection
  usually go through; too many too fast triggers Amazon's bot check. If that
  happens, wait a few minutes — repeated searches are cached, so re-runs are free.
- **Fragile**: if Amazon changes their HTML, the regex parsing may need a tweak.
- **Personal, low-volume use only.** Scraping is against Amazon's Terms of Service.
  Don't automate it heavily or build a product on it.

## License

This project is open source and available under the [MIT License](LICENSE).
