# Governance Gate (native-agent build) — how to test it live

Test target: **`papaya-governance-gate-agent.n8n.json`**. Everything except the live
n8n run is already verified — Code-node JS (`node --check`), node configs (n8n MCP
`validate_node_config`), graph integrity, and the full Pinecone RAG path (all three
namespaces, live). What's left is the one thing a native AI Agent can't be dry-run:
the agent loop itself. Budget ~15 min.

---

## 1. Import + credentials (one-time)

1. **Import** `papaya-governance-gate-agent.n8n.json` into n8n.
2. Set **four credentials**:
   | Node(s) | Credential | Type |
   |---|---|---|
   | `Notion Trigger…`, both `Deliver…` nodes, `Get Draft Body` | Notion | `notionApi` |
   | `gpt-5.5`, `gpt-4.1-mini`, `gpt-4.1-nano`, `gemini-3.5-flash`, `gemini-3.1-pro-preview` (5 model nodes — main agent + fallback, kb_agent + fallback, output-parser auto-fixer) | OpenRouter | `openRouterApi` |
   | `search_facts`, `search_published`, `search_claims` | Pinecone | `pineconeApi` (Api-Key header). Key: the disposable one in `pinecone/` scripts — **delete it from the Pinecone dashboard when the assignment is done.** |
3. On the `Notion Trigger…` node, set **`databaseId`** to the content-calendar DB (replace `SET_DATABASE_ID_AT_IMPORT`).
4. Nothing else to configure — the context layer is embedded, model slugs are set, RAG endpoints are wired.

## 2. Demo Notion DB (one-time)

Create a Notion database with:
- A **title** property (page name).
- A **`Status`** property (any type) with at least the options `Draft` and `In Review`. The gate fires only on these.
- A **`Compliance Status`** **select** property with options **`Ready`**, **`Needs Review`**, **`Needs Content`**. This is where the verdict lands (property name must match `Compliance Status` exactly).

Then add the four fixture pages below (each: set title, paste the body, leave `Status` = `Draft`).

## 3. The fixtures + expected verdicts

Paste each as the page body. The gate extracts claims, verifies against the fact
layer + published posts + approved/banned claims, and writes `Compliance Status` +
an appended audit block.

### Fixture A — Clean → `Ready`
> Papaya Global provides Employer of Record services in 180 countries and runs native payroll in 120+ countries. EOR pricing starts at $499 per employee per month. About 90% of payments settle in real time, with a 99.5% delivery rate. Papaya was founded in 2016 by Eynat Guez, Ruben Drong, and Ofer Herman.

**Expect:** `Ready`. Every claim traces to an approved source (context layer + `claims` namespace). Audit block: "All claims trace to an approved source."

### Fixture B — Hallucinated figure → `Needs Review`
> Papaya Global processes payroll for over 8,000 enterprise customers and guarantees a 100% on-time payment rate across every one of the 200 countries it serves.

**Expect:** `Needs Review`.
- "8,000 customers" → `contradicts_approved_claim` / `banned_claim` (approved figure is 2,000+; banned to inflate).
- "100% on-time payment rate" → `hallucinated_fact` (verified figure is 99.5% delivery).
- "200 countries" → `hallucinated_fact` / `jurisdiction_overreach` (verified is 180).

### Fixture C — Banned claim → `Needs Review`
> Papaya is the only payroll platform offering real-time settlement, backed by unlimited liability coverage on every hire.

**Expect:** `Needs Review`.
- "real-time settlement" → `banned_claim` (correct form: "about 90% settled in real time" — `search_claims` returns the banned record + `correct_form`).
- "the only … platform" → `banned_claim` (invented head-to-head superlative).
- "unlimited liability coverage" → `liability_language` (Papaya caps liability).

### Fixture D — Planted contradiction (the headline catch) → `Needs Review`
> Great news for global teams: Papaya Global now offers Employer of Record hiring starting at just $299 per employee per month — the lowest enterprise EOR price on the market.

**Expect:** `Needs Review`.
- "$299 per employee per month" → `contradicts_approved_claim` (approved canonical claim is $499; `search_claims` returns it) **and/or** `contradicts_prior_publication` (Papaya's own `eor-pricing-cost` post; `search_published` returns it). This is the catch the fact-only build could not make — the price is internally plausible but disagrees with what Papaya already published.
- "the lowest enterprise EOR price on the market" → `banned_claim` (unqualified superlative).

### (Optional) Fixture E — Empty page → `Needs Content`
Leave the body empty, set `Status` = `Draft`. **Expect:** `Needs Content`, no model call (the `Empty draft?` IF routes it around the agent).

### (Optional) Non-reviewable status → no-op
Set a page to `Status` = `Published`. **Expect:** the `Only Draft / In Review` filter drops it — no write-back, no run.

## 4. Run it

1. On the `Notion Trigger…` node, click **"Fetch Test Event"** to pull the page you just moved to `Draft` (instant, no poll wait).
2. **Execute** the workflow.
3. Watch: entry guardrail → filter → get body → assemble → the agent (it will call `kb_agent`, which calls `search_facts` / `search_published` / `search_claims`) → decision → the two Notion writes.
4. Check the Notion page: `Compliance Status` set, and an appended audit block — one line per flag with `[risk_category] verdict: "claim" | source: evidence`.

## 5. What to watch for (and the honest caveats)

- **Token usage** shows in n8n's execution view (agent + nested sub-agent), not as a clean per-stage log — the accepted native-agent tradeoff. The `Observability — Compile Run Report` node captures the verdict summary + whatever usage the agent exposes.
- **If the agent's structured output is malformed**, the `Verdict Schema` parser's `autoFix` is on: it makes one repair call to its own dedicated model (`gpt-4.1-nano` — cheap, since it's only reformatting, never re-judging) before giving up. If that repair also fails, it fails loud rather than passing broken data downstream.
- **If Pinecone is briefly unavailable**, the sub-agent reports the KB as silent, the main agent marks affected claims `unverifiable`, and the decision forces `Needs Review` — additive assurance, never a single point of failure.
- **A true threaded Notion comment** (vs. the appended block) is a one-node production swap: an HTTP Request node POSTing to Notion `/v1/comments` with the same credential.
