<div align="center">

# DartLens

**Lets Claude and Codex read Korean DART filings themselves before they answer**

[leetkey.kr/en](https://leetkey.kr/en/) | [한국어](https://github.com/Johnhyeon/dartlens-mcp/blob/main/README.md) | [Patch notes](https://github.com/Johnhyeon/dartlens-mcp/blob/main/PATCHNOTES.md)

</div>

---

Show an AI a filing PDF or a screenshot of financial statements and it easily misreads the numbers. Mix up millions and thousands of won, a 3-month figure and a year-to-date one, or consolidated and separate statements, and the whole answer is wrong.

With DartLens connected, the AI pulls filing lists, original filings and structured financial statements straight from DART, Korea's FSS disclosure system. **It states which report, which table and which row it read.**

DartLens is part of the [LeetKit](https://leetkey.kr/en/) FULL Package, used together with StockLens (prices and flows) and TelegramLens (Telegram stock chatter).

**Ask in English, get answers in English.** Filings are in Korean; the AI translates as it answers.

## A real answer

> What is Hyundai E&C's order backlog?

**Hyundai E&C: order backlog** `From the original DART annual report`

| Point | Backlog | Source |
|---|---:|---|
| End of 2025 | KRW 95.70 trillion | Annual report (2025.12), table "(unit: KRW million)", total row, 1 of 58 rows in the original |
| End of 2024 | Left out | A table had no unit label, so no total was computed |

Misread the unit and you are off by 100x or 100,000x. DartLens reads the total row as filed, and when it cannot be sure of the unit it leaves the number out and says so. (Labels translated from the Korean output.)

## What you can ask

```
What did Samsung Electronics file in the last month?
Summarize recent 5% ownership changes and insider trades at EcoPro BM
Find the parts of LG Energy Solution's annual report that talk about orders
```

```
Show SK hynix quarterly revenue and operating profit for the last 3 years
List companies whose operating profit jumped this earnings season and save it to Excel
```

## What it covers

- **Filings:** list by period and type, excerpts of short filings, table of contents and links for long reports, keyword search inside the text
- **Financial statements:** key accounts (revenue, operating profit, net income, assets, liabilities, equity) across three periods, plus full statements when needed
- **Order backlog:** backlog and contract balance trends from annual, quarterly and half-year reports
- **Ownership:** 5% major-holder changes and insider holdings, money moves that price data does not show
- **Earnings season scan:** sweep the companies that reported, tabulate, save to Excel

11 tools in all.

## Labels come first

- **It says where it read.** Report, table and row, so you can check the original.
- **No unit, no number.** If the unit is uncertain, the value is left out and marked as such.
- **3-month and year-to-date are kept apart,** in separate columns for quarterly and half-year results.
- **Truncated lists say so.** "8 shown of 11", never presented as the full list. "No such filing" is said only after checking everything.

## Where it runs

| | |
|---|---|
| AI apps | Claude Desktop, Codex (ChatGPT account), Claude Code |
| OS | Windows, macOS |
| Not supported | Claude.ai on the web (it cannot connect to your PC) |

The AI app's own subscription is separate from LeetKit.

## Setup and pricing

- **Setup:** install with a button in LeetKit Manager and paste your license key. No commands.
- **DART API key (free):** get one at [opendart.fss.or.kr](https://opendart.fss.or.kr) and paste it into the DartLens card's [Activate] in the Manager. Up to 1,000 calls a minute and 20,000 a day. The key is kept in your OS's secure store (Windows Credential Manager, macOS Keychain).
- **Trial:** 14 days free with all three Lenses, email only, no card.
- **Pricing:** one-time payment, no subscription. DartLens is not sold alone; it comes in the LeetKit FULL Package. Checkout is Korean; overseas cards may not work, so email us first.

Trial and prices: **[leetkey.kr/en](https://leetkey.kr/en/)**

## Not investment advice

DartLens is a data tool that lets your AI look up public disclosure data. It is not an investment advisory, discretionary management or stock recommendation service. It does not recommend buying or selling any security and has no order execution. Data can be delayed or wrong depending on the source, and AI answers are for reference only. Investment decisions and their outcomes are your own responsibility.

## License

Proprietary software. The source code is not public, and a valid license key is required. See [LICENSE](https://github.com/Johnhyeon/dartlens-mcp/blob/main/LICENSE). This repository holds the overview and patch notes only.

Contact: support@leetkey.kr · Made by Leetkey Lab (리트키랩)
