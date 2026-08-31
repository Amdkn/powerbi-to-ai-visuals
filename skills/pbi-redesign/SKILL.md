---
name: pbi-redesign
description: Turn an extracted Power BI model into a dashboard worth acting on. Use when someone wants more than a like-for-like rebuild - a strategic view, an executive summary, "what does this data actually say". Runs as a staged workflow, analyst then strategist then designer then builder then critic, with the evidence computed deterministically and only the judgment left to the agent.
---

# Design the report they should have had

A faithful replica answers "what did the report say". This answers "what should
somebody do on Monday". Those are different questions and they want a different
page.

The split that makes this work: **evidence is computed, judgment is written.**
`tools/analyze_model.py` produces facts. You produce `narrative.json`. The
builder merges them. Nothing on the finished page asserts anything that is not
traceable to a query or to a line you wrote deliberately.

## Stage 1 - analyst

```bash
python3 tools/analyze_model.py --model build/model -o build/analysis
```

Writes `findings.json` and `findings.md`: detected roles, period coverage,
totals, by-year growth and margin, mix, concentration, year-on-year movers,
data-quality findings, and which tables the source report never touches.

Read `findings.md` before anything else, and check the roles first. They are
regex guesses over column names and every one is recorded with its evidence. If
the revenue column or the date table is wrong, everything downstream is wrong:
fix it with `--roles roles.json` and rerun.

## Stage 2 - strategist

Decide what the page is *for*, from the findings and not from habit. Ask:

- What is the largest number nobody is looking at?
- Which fact changes a decision, rather than describing the past?
- What does the existing report structurally *cannot* show? Unreferenced
  tables and unused columns are the cheapest place to look; on the worked
  example the source report never used the cost column or the channel column,
  which between them hid that 73% of revenue produced almost no gross profit.
- What would make a number on this page wrong if a reader did not know it?
  Partial periods, placeholder members and referential-integrity gaps all
  belong at the top of the page, not in a footnote.

Then rank. A dashboard that says eight things says nothing.

## Stage 3 - designer

Choose the shape before writing markup:

- **One headline.** The most consequential finding, in a sentence, at the top.
- **Comparisons that are honest.** If the latest period is short, compare
  like for like or say plainly that you did not.
- **Filters only where the maths survives them.** In the worked example the
  filters stop at fiscal year and channel because both are order-level, so
  distinct order counts stay exact; product appears as a breakdown instead. A
  filter that silently breaks a KPI is worse than no filter.
- **Every card earns its place** with a "so what", not a restatement of its
  own axis labels.
- **Caveats above the numbers**, not below them.

## Stage 4 - builder

Write `build/analysis/narrative.json`:

```json
{
  "title": "...", "headline": "...", "standfirst": "...",
  "readThisFirst": ["caveats that change how the numbers read"],
  "cards": { "kpis": {"so_what": "..."},
             "trend": {"title": "...", "so_what": "..."} },
  "actions": [{"title": "...", "why": "...", "evidence": "the query behind it"}],
  "authored_by": "...", "method": "..."
}
```

Card keys the template knows: `kpis`, `trend`, `channel`, `category`, `movers`,
`pareto`, `economics`, `quality`, `actions`. Then:

```bash
python3 tools/strategic_html.py --model build/model \
  --findings build/analysis/findings.json \
  --narrative build/analysis/narrative.json \
  --out build/strategic
```

For a different set of cards, edit `tools/templates/strategic.js` and add the
cube it needs to `tools/strategic_html.py`. Keep cubes small: the page ships
them inline, and every one should be reachable from the queries printed in the
page footer.

## Stage 5 - critic

Before handing it over, go back through the finished page and check:

- **Every number reproduces.** Re-run a sample through `pbi-chat`. The
  dashboard prints its own SQL in the footer; run it.
- **No claim outruns its evidence.** "Margin is falling" needs the margin
  series. "Because of discounting" needs the discount data - and if the model
  does not have it, say "consistent with" rather than "because".
- **The caveats survived.** A partial period or a placeholder member that got
  dropped during design is the failure mode that matters, because the page then
  looks more certain than the data is.
- **Percentages agree with each other** across the headline, the cards and the
  actions. They are written by hand and they drift.

Say what you would need to go further: the worked example cannot tell a pricing
problem from a cost-basis problem, and the page says so rather than picking one.
