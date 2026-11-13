# Report data (report.json)

The interactive report is `assets/report-template.html` filled with one JSON object. Write the JSON only from offers you verified in this run. `scripts/build_report.py` validates it and refuses to build when a rule below is broken. Fix the data; never bend it to pass.


## Top level

| Field | Required | Meaning |
|---|---|---|
| `schema_version` | yes | `1` |
| `checked_at` | yes | Date the prices were read, `YYYY-MM-DD` |
| `query.text` | yes | What the user asked for, in Hebrew, one line |
| `query.input_type` | yes | `image` \| `link` \| `text` \| `barcode` |
| `query.reference_price` | no | `{amount, currency}`: the price the user saw (their link or what they told you). Never estimated. Enables the "₪X פחות" opening number |
| `query.assumptions` | no | Short Hebrew chips, e.g. `["משלוח לישראל", "מוצר חדש"]` |
| `product.title` | yes | The product as understood |
| `product.subtitle`, `product.tags` | no | Category / brand line; 2–4 short attribute tags. Say `גודל לא ידוע` rather than guessing |
| `product.image` | no | The user's photo: local file path (relative to the JSON) or `https` URL. `null` if none |
| `product.image_alt` | no | Hebrew alt text |
| `product.insight` | no | One sentence on what matters most for the match |
| `summary.headline`, `summary.text` | no | The finding, plain Hebrew. Same wording rules as the chat answer |
| `top_pick` | no | `{ "offer_id": "...", "basis": "total" \| "item_price" }`. See below |
| `offers` | yes | List, best first. `[]` is a valid honest result. Max 12 |
| `rejected` | no | Every listing you really looked at in this run and dropped: `{ "store": "...", "why": "blocked", "image": "https://..." }`. `why` is one of `blocked` (page would not open), `different_product`, `out_of_stock`, `used` (refurbished / open box), `conditional_price` (Eilat, club, coupon), `duplicate` (same shop, other colour or page), `no_shipping`, `not_a_shop` (aggregator, blog, unidentified seller), `other`. `image` optional. These fill the opening animation. Never pad this list |
| `notes` | no | Extra caveats shown under the cards |
| `demo` | no | `true` only for sample reports that are not a fresh search |

## Offer

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Unique short string |
| `store` | yes | Retailer name as shown on its site |
| `title` | yes | Product name in Hebrew (keep model names in Latin letters) |
| `spec` | no | Size / brand / variant line |
| `url` | yes | The real `http(s)` product page you opened. No tracking or affiliate parameters |
| `image` | no | Product image from the retailer page: `https` URL or local file. Never a generated image |
| `match_type` | yes | `exact` \| `similar` \| `unconfirmed` (verification.md, "Honest result states") |
| `match_reason` | yes | Why it got that match type |
| `differences` | for `similar` | What differs from the user's product |
| `price` | yes | `{ "amount": 318, "currency": "ILS" }` in the shop's own currency, or `null` if not read from the product page (then `match_type` must be `unconfirmed`) |
| `shipping.status` | yes | `known` (with `amount`, `currency`) \| `free` (with `note` quoting the shop) \| `unknown` \| `unavailable` |
| `shipping.note` | no | Short caveat, e.g. `ישראל לא אומתה` |
| `total` | yes | `{amount, currency}` only when every mandatory cost is known and in the price currency; otherwise `null` |
| `availability` | yes | `in_stock` only if the page says so for this variant \| `out_of_stock` \| `unknown` |
| `availability_note`, `condition`, `warranty` | no | Shown in the details dialog |
| `evidence` | no | Short strings: SKU, barcode, model, where the price was read |

## What the builder enforces

- Unknown is not zero and not free. `free` needs a quoted note; `unknown`/`unavailable` carry no amount.
- No `total` unless shipping is `known` or `free`, and never below price + shipping.
- A `similar` offer must state its differences. An offer without a verified price is `unconfirmed`.
- `top_pick` draws the "cheapest verified" ribbon and is checked against the numbers:
  - `total`: the offer has the lowest verified total among available offers of the same match type.
  - `item_price`: lowest item price, allowed only when no comparable offer has a full total. The ribbon then says shipping is not verified.
  - Rejected when the compared offers are in different currencies, or the offer is unconfirmed or out of stock. When in doubt, omit `top_pick`; the report then states that no cheapest option was declared.
- Links must be `http(s)`. Images must be real JPEG/PNG/WebP/GIF/AVIF, local or `https`.

## Opening animation

The report opens with a ~13 second show built from this data only: the user's photo is scanned, every listing that was looked at swirls in around it, the dropped ones are pulled into piles by reason, the verified offers become cards, and one big number lands. The number is chosen by the template: savings against `query.reference_price` (exact `top_pick`, same currency), else the gap between the cheapest and the most expensive verified offer of the same exact product, else the `top_pick` price, else the count of verified offers. The more listings honestly recorded, the better the show. Record `rejected` and `reference_price` faithfully; they are what makes the show true.

## Build

```
python3 scripts/build_report.py report.json -o report.html
```

Output is one self-contained HTML file (images embedded; a remote image that cannot be downloaded stays a link and falls back to a placeholder). `--check` validates without writing. `--no-fetch` skips image downloads.

No code execution in the host? Copy `assets/report-template.html`, and replace the `/*__REPORT_DATA__*/` marker with the JSON, using `https` image URLs and writing every `<` inside the JSON as `<`. Apply the rules above by hand.
