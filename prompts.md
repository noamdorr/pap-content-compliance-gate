# Papaya Governance Gate — Prompts & Deterministic Logic

The two LLM prompts and the deterministic (no-LLM) logic, extracted from
`papaya-governance-gate.n8n.json` for review without opening n8n. Model routing:
claim extraction = `anthropic/claude-haiku-4.5` ($1/$5 per MTok); verification =
`anthropic/claude-sonnet-5` ($2/$10 per MTok); every decision is deterministic JS.

---

## Verification + taxonomy prompt (Sonnet)

**System:**

> You are a skeptical compliance reviewer for Papaya Global, a payroll/EOR company that legally owns liability across 160+ jurisdictions. You do not write or improve copy. For every claim you decide only whether it is provable from the approved evidence, and if not, what KIND of risk it is.

**User (templated):**

```
Verify each claim below against TWO evidence sources. Source #1 is the verified fact context layer. Source #2 is Papaya's OWN already-published content (a claim may be internally plausible yet CONTRADICT what Papaya already said publicly - that is itself a risk).

=== SOURCE #1: FACT CONTEXT LAYER (ground truth; if a fact is not here, it is not admissible) ===
{{ context layer }}

=== SOURCE #2: ALREADY PUBLISHED (Papaya's own recent posts) ===
{{ prior_publications retrieved from Pinecone, or "(none retrieved)" }}

=== CLAIMS TO VERIFY ===
{{ claims array from Gate 1 }}

For EACH claim return an object with:
- id, claim (copy through unchanged)
- verdict: "supported" | "contradicted" | "unverifiable"
- evidence: a verbatim quote from whichever source backs or breaks it (empty string if none exists)
- source: "context_layer" | "prior_publication" | "none"
- confidence: a number 0..1
- risk_category: one of "none" (only when supported), "hallucinated_fact", "stale_figure", "banned_claim", "contradicts_prior_publication", "jurisdiction_overreach", "liability_language"

Rules:
- A claim is supported ONLY if a source explicitly backs it. Never reason from general knowledge.
- Check the context layer's banned_claims list explicitly; tag any match banned_claim.
- If a claim disagrees with a Source #2 quote, verdict=contradicted, risk_category=contradicts_prior_publication, and quote the prior post in evidence.
- A claim that overreaches on country/jurisdiction coverage the layer does not support is jurisdiction_overreach; uncapped-liability phrasing is liability_language; a figure the layer marks as dated is stale_figure.
Return a JSON array only. No prose, no markdown fences.
```

The verifier sees the **full** context layer (not a triage extract) plus the retrieved prior-publication chunks, so a weak upstream retrieval can't hide a real fact or wave through a fake one.

---

## Claim-extraction prompt (Haiku)

**System:**

> You are the claim-spotting stage of a verification pipeline. You extract, you don't judge.

**User (templated):**

```
DRAFT ABOUT TO GO TO REVIEW:
<draft>
{{ draft_text — the Notion page body }}
</draft>

Pull out every checkable factual claim: numbers, dates, prices, product capabilities, company facts (founders, funding, customers, certifications, partnerships), superlatives ("the only," "the first," "the largest"), and any named third party.

Leave out opinions and generic industry statements ("payroll is complicated"). If you're not sure whether something counts, include it -- a missed claim here becomes an unchecked hallucination downstream.

Return JSON only:
[{"id": 1, "claim": "..."}]
Return [] if there's nothing to check.
```

Deliberately over-inclusive: a missed claim is an unchecked claim. Runs on the cheap model because spotting is mechanical; the judgment happens in the verifier.

---

## Deterministic logic (no LLM)

Everything that decides publish-vs-hold is plain JavaScript. The LLM is never asked to do arithmetic or make the go/no-go call.

**Entry guardrail (`Guardrail — Entry Conditions`):** proceeds only when the Notion `Status` is `Draft` or `In Review`; any other status is a no-op skip (not an error). Extracts `page_id`, `page_title`, `status` from the raw Notion page object.

**Context presence + freshness (`Assemble Draft + Load Context Layer`):**
- Empty/missing context layer (< 500 chars) → hard stop. No context, no verdict.
- Empty draft (< 40 chars of body) → short-circuit to `Needs Content`, never a fake pass.
- Reads the `last_verified` / `compiled` stamp out of the context layer; context older than 30 days sets `context_stale = true`.

**Decision rule (`Parent — Decide Ready / Needs Review`):**
```
if (_needs_content)                          -> "Needs Content"
else if (any claim.verdict !== 'supported')  -> "Needs Review"
else if (context_stale)                      -> "Needs Review"   // even when all claims pass
else                                         -> "Ready"
```

**Audit annotation format** (ASCII hyphens only — anti-AI-tell discipline). One header line, then one line per flagged claim:
```
Compliance gate: Needs Review  (3 claims checked)
- [contradicts_prior_publication] contradicted: "EOR pricing starts at $299/mo"  |  prior published post: EOR from $499 per employee per month
- [banned_claim] contradicted: "the only payroll platform offering stablecoin payouts"  |  fact layer: do not invent head-to-head superlatives
```
A clean draft gets a single "All claims trace to an approved source. Cleared for a human to publish." line.

**RAG degradation:** if Pinecone is unavailable, `contradiction_check` is stamped `skipped (RAG unavailable)`, the run continues on the fact layer alone, and the result is forced to `Needs Review` — additive assurance, never a single point of failure.

**Delivery:** `Compliance Status` (select property) + the audit appended as a paragraph block on the page. The n8n Notion node has no comment resource; a true threaded comment is a one-node production swap — an HTTP Request node POSTing to Notion's `/v1/comments` with the same credential.

**Observability:** per-stage `{model, input_tokens, output_tokens, cost_usd, duration_s}` rolled into `verification_report` with `total_cost_usd` + `total_tokens`. Two LLM stages (Haiku extract + Sonnet verify), so a run is materially cheaper than a five-stage generate-and-check pipeline — the Part 4 cost argument.
