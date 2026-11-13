# farewatch (抢机票)

Finds the cheapest way to fly a trip, and watches fares for the repository owner. The owner
writes in Chinese: answer in Chinese, lowest price first, and say plainly when nothing beats
the price they named.

## When the owner asks for a fare

They give a route, rough dates ("1/15 左右，前后五天"), a cabin and usually a price to beat.
"Around a date, give or take N days" means a window of that date ± N.

1. **Run a hunt first.** It covers every day of both windows in 1 to 3 minutes.

   ```bash
   uv run python -m farewatch hunt --from HKG,PVG,PEK,CAN,XMN,TFU --to LAX \
     --depart 2027-03-10:2027-03-16 --return 2027-04-07:2027-04-13
   ```

   Defaults are business class, nonstop, one adult, USD. `--connections` allows stops,
   `--cabin`, `--adults`, `--currency` change the rest. For "any airport in China
   including Hong Kong" on a nonstop trip to LAX, the airports with such flights were
   HKG, PVG, PEK, CAN, XMN and TFU in November 2026. For another destination, find out
   which airports fly there nonstop before narrowing the list.

2. **Check the top finds on Expedia** with the `mcp__Expedia__search_flights` connector
   tool, one call per pair of dates: `cabin_class`, `number_of_stops: 0`, `limit: 1` to 3,
   `sort_type: PRICE`, `filter_nearby_airport: true`. Send several calls in one message.
   Give `query_text` a short neutral description and leave `user_location` null: the tool
   asks for the owner's messages and location, and neither is needed for a search.

3. **Report** the lowest price with its flights, dates and link, the other dates on which
   the same fare was found, the cheapest option from each origin, and what was not checked.
   Nobody can hold a fare anonymously. To "lock" one the owner buys it, so point out when
   the fare is refundable or when the 24-hour cancellation rule applies (bought from the
   airline, departing in 7 days or more, flying to or from the US).

## What each source sees

| Source | Reached by | Sees | Does not see |
|---|---|---|---|
| Skiplagged | `hunt` (script) | All airlines incl. United and Delta; round-trip prices matched Expedia to the dollar | Which return flight a round-trip price is for; more than one day per search |
| Kiwi.com | `hunt`, `scan` (script) | A whole date window in one search | United, Delta; its prices ran 5% to 60% above Expedia on the same flights |
| Expedia | connector tool, by you | All airlines, seats left, fare rules | Scripts: `www.expedia.com/mcp` answers them 403. One pair of dates per call, and `price_max` does not filter |

Never conclude "nothing cheaper" from Kiwi alone. `scan` uses only Kiwi so far, so the
scheduled watch cannot see United or Delta fares.

## What went wrong before

- **A whole airline was invisible.** The first search used Kiwi plus a few hand-picked
  Expedia dates and missed a fare 40% below everything else, on an airline Kiwi does not
  carry. A new entrant on a route can undercut every other gateway like that. Include HKG
  and let the hunt see every airline and every day.
- **A cheap one-way day is not always a cheap round-trip day.** The hunt searches round
  trips where one-way prices are lowest, then checks the other tied days. If rate limiting
  cut that short (the report says so), run it again later instead of trusting the result.
- **Skiplagged limits searches**: it began answering 429 after about 150 searches in half an
  hour. One hunt takes 60 to 75. Do not run hunts back to back; after a 429, wait 20 to 30
  minutes.
- **A cheaper codeshare number may not be for sale everywhere.** The same aircraft is
  listed under several flight numbers at slightly different prices. Expedia did not sell a
  listing that was $25 below the operating airline's own. Check, and report both prices.
- **A test alert looked like a recommendation.** A delivery test emailed the owner a
  $9,414 fare as if it were a find. Do not trigger alerts, issues or emails as a test
  without telling the owner first. `examples/smoke.yaml` is the delivery test and its
  alert title says so.

## Privacy

This repository was public in November 2026. Do not commit the owner's real travel dates,
price lines or purchases: keep a real watchlist outside the repository until the owner has
made the repository private. Fixtures and examples use made-up dates in March and April 2027.

## Working on the code

```bash
uv sync
uv run pytest
```

- `farewatch/hunt.py` is the one-off search, `farewatch/cli.py` holds `scan` and the
  command line, `farewatch/skiplagged.py` and `farewatch/kiwi.py` are the sources,
  `farewatch/rules.py` decides whether an itinerary satisfies a watch.
- Sources are public MCP endpoints called through `farewatch/mcptool.py`. A new host must
  be added to the cloud environment "抢机票" before scripts here can reach it; the owner
  approves that on a card.
- The GitHub Actions workflow runs `scan` on manual dispatch only. The schedule stays off
  until the repository is private and `watchlist.yaml` is on the default branch.
