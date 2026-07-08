# Papaya Compliance Governance Gate — Design Spec
### Part 3: Design & Build a Marketing AI Agent (revised direction)

> Build complete and dry-run verified (`papaya-governance-gate.n8n.json`, 22 nodes) — see `Papaya-Part3-Governance-Gate-PLAN.md`. Delivery is via appended page block, not a threaded comment (n8n's Notion node has no comment resource); a true comment is a documented one-node HTTP swap.
>
> **Submitted build = the native-agent rebuild (`papaya-governance-gate-agent.n8n.json`, 26 nodes).** Same locked logic, restructured around n8n's native AI Agent node: one judgment-model agent (`gpt-5.5`, with a `gemini-3.1-pro-preview` fallback via n8n's built-in model-fallback slot) that extracts every claim *and* verifies each, a Structured Output Parser enforcing the per-claim verdict schema (with `gpt-4.1-nano` as its dedicated auto-fix model for malformed JSON), and a `kb_agent` sub-agent (`gpt-4.1-mini`, with a `gemini-3.5-flash` fallback, separate context window) exposing three Pinecone RAG tools — `search_facts` (namespace `docs`), `search_published` (namespace `published`), and `search_claims` (the new approved/banned canonical-claims namespace). Every model call is routed through OpenRouter, and every reasoning-critical stage carries a same-tier fallback from a second provider — a model or provider outage degrades the run, it doesn't kill it. The HTTP+Code build stays as the dry-run-verified backup. The two knowing tradeoffs of the switch are recorded in §9.

**What it is:** an agent that sits between "draft" and "published" and refuses to let a claim it can't prove go out under Papaya's name. It is triggered the moment a marketer moves a page to `Draft` in Notion, reads the draft, checks every claim against two sources of truth — Papaya's verified fact layer and Papaya's own prior published content — and writes a categorized compliance verdict back into Notion as a comment, with quoted evidence. It never publishes. It makes the human reviewer faster and harder to fool.

**The inversion — agent-in-the-loop, not human-in-the-loop:** the obvious build is "AI writes, human approves" — the human is the gate. This flips it. The human writes; the *agent* is the gate. The agent inserts itself into the authoring loop the marketer already lives in (Notion) as a tireless first-pass compliance reviewer, and the human governs only the exceptions it raises. For a company whose entire pitch is "we own the liability" across 160+ jurisdictions, the risk that matters isn't that an AI wrote something clumsy — it's that *anyone* (PMM, freelancer, agency) publishes a claim that's stale, contradicts a prior post, or overreaches on compliance, under the brand of the company that legally owns the outcome.

**Why this and not content generation:** a content-generation agent is the median answer, and it's honestly out of scope for a build with an eval budget — every sub-agent and every tool (draft, brand-voice, SEO, humanizer, research) is a separate thing to test and evaluate, which is a research project, not a component. Narrowing to the governance gate is the deliberate senior call: one component, one clear contract, testable in isolation, and it maps directly to the assignment's "high impact" bar — it's the difference between a cute automation and a control that reduces legal/brand exposure at the exact moment exposure is created. The gate was already the strongest box in the earlier build; this design makes it the whole build instead of the insurance policy on a generator.

---

## 1. Trigger & entry conditions

- **Trigger:** a Notion page's `Status` property changes to `Draft` (or `In Review`) inside the content-calendar database. Two viable mechanisms, decided at build time:
  - **Notion automation → n8n Webhook node** (recommended for the demo): Notion's native per-database automation fires instantly on the status change and POSTs the page to an n8n Webhook node. Instant, and it makes the "it runs the second I move the card" demo beat land.
  - **n8n Notion Trigger node** (zero-config fallback): polls the database on an interval for pages updated since the last run. Simpler to import, but adds poll latency — worse for a live demo.
- **Entry conditions (hard gate, before any model call):**
  1. A non-empty fact context layer is loadable. No context, no verdict — the run stops rather than "approving" against nothing.
  2. The triggering page has readable body content. An empty draft short-circuits to a `needs_content` status, not a fake pass.

## 2. LLM selection & justification

Routed through OpenRouter so each model string is a one-line swap per node (same discipline as the prior build), model slugs confirmed live against OpenRouter's catalog at build time.

| Stage | Model | Why |
|---|---|---|
| Claim extraction | `anthropic/claude-haiku-4.5` | Mechanical: pull every checkable claim out of the draft. Over-inclusive on purpose; the cheap model is correct here. |
| Skeptical verification + risk categorization | `anthropic/claude-sonnet-5` | Judgment: a wrong "supported" verdict is the worst failure in the system — it turns "unchecked" into "confidently wrong" with Papaya's name on it. This is also where the `risk_category` taxonomy is assigned. Worth the smart model. |
| Parent routing (ready / needs_review) | none — plain JS | Deterministic. The LLM never does the arithmetic on how many claims failed. |

`anthropic/claude-opus-4.8` stays reserved for strategy-grade work — not on this path.

**Native-agent restructure (submitted build).** The table above describes the two *logical* jobs; the actual models wired into the submitted build are below. In the native-agent build the two jobs collapse onto one **judgment-model agent** that extracts every claim *and* verifies each in a single ReAct loop — extraction is folded into the judgment model rather than run as a separate cheap stage. The cheap-model economy doesn't disappear; it relocates to the **`kb_agent` sub-agent**, which does the mechanical retrieval/synthesis against the three Pinecone namespaces so the expensive model never reasons over raw KB text. The deterministic parent routing is unchanged.

**Models actually wired (OpenRouter, each with a same-tier fallback via n8n's `needsFallback` slot):**

| Node | Primary | Fallback | Job |
|---|---|---|---|
| Compliance Governance Agent | `openai/gpt-5.5` | `google/gemini-3.1-pro-preview` | extract + verify (judgment) |
| kb_agent | `openai/gpt-4.1-mini` | `google/gemini-3.5-flash` | KB retrieval/synthesis (mechanical) |
| Verdict Schema (output parser) | `openai/gpt-4.1-nano` | — | auto-fix malformed JSON only, never judgment |

Provider diversity is deliberate here, not just a model-tier choice: a same-tier fallback from a *different* provider (OpenAI ↔ Google) means an OpenAI outage doesn't take the compliance gate down with it — which matters more for a governance control than for a content generator.

## 3. Prompt architecture & context structuring

Two LLM stages, one job each — the surviving half of the earlier pipeline, extended:

1. **Claim extraction** (Haiku): pull every checkable claim out of the *submitted draft* — numbers, dates, product capability, company facts, superlatives, named third parties, jurisdiction/compliance assertions, liability language. Over-inclusive; a missed claim is an unchecked claim.
2. **Skeptical verification + categorization** (Sonnet): sees the **full** fact context layer AND the **retrieved prior-publication chunks** (see §4). For each claim, returns:
   - `verdict`: `supported` / `contradicted` / `unverifiable`
   - `evidence`: a verbatim quote from whichever source backs (or breaks) it
   - `source`: `context_layer` or a specific prior post + URL
   - `confidence`: 0–1
   - `risk_category` (the governance taxonomy — see below)
   - Explicitly checks the `banned_claims` list.

Context is the same compiled markdown layer from Part 2B (`company_facts`, `products`, `positioning`, `icp_personas`, `proof_points`, `partnerships_and_news`, `competitors`, `banned_claims`, `tone_rules`, `brand_visual`), each fact source-tagged, with the `last_verified` stamp the freshness guardrail reads.

### The taxonomy (what makes this "governance," not "fact-check")

Every flagged claim carries a `risk_category`, so a reviewer sees *what kind* of risk, not just pass/fail:

| `risk_category` | Caught by | Example |
|---|---|---|
| `hallucinated_fact` | context layer | a stat or feature not present in the verified layer |
| `stale_figure` | context layer + freshness | the $3.7B / Sept-2021 valuation stated as current |
| `banned_claim` | `banned_claims` list | "real-time settlement" (vs. the real "90% settled in real time"); "green" branding; customer counts beyond the approved figure |
| `contradicts_prior_publication` | published-content RAG (`published`) | new draft states an EOR price that disagrees with the live pricing post |
| `contradicts_approved_claim` | approved-claims RAG (`claims`) | new draft disagrees with a canonical approved claim (e.g. draft says EOR from $299 vs. the approved $499) |
| `jurisdiction_overreach` | context layer | claiming coverage/compliance in a country the layer doesn't support |
| `liability_language` | `banned_claims` | "unlimited" / uncapped-liability phrasing on a company that caps it |

The taxonomy is a schema addition to the existing verification report — same verification node, one more field per claim. It is the difference between "this claim is wrong" and "this is a *contract-risk* claim you must escalate."

## 4. Data sources & integrations

- **Fact context layer** (source of truth #1): the compiled `papaya-context-source.md` from Part 2B, embedded in the workflow for a self-contained import. Catches hallucinations, stale figures, banned claims, jurisdiction overreach.
- **Published-content RAG** (source of truth #2): a Pinecone index (`papaya-kb`, namespace `published`) seeded with Papaya's 30 most-recent live blog posts (186 chunks, integrated embedding, `llama-text-embed-v2`). Queried with the draft's topic; the top chunks are handed to the verifier so it can catch a draft that **contradicts what Papaya already said publicly**. This is the "the old blog says X, the new brief says Y" capability — folded into the same verification call as a second evidence source, not built as a separate system.
- **Approved/banned canonical claims** (source of truth #3): a third Pinecone namespace (`papaya-kb`, namespace `claims`, 24 records — 14 approved + 10 banned, seeded by `pinecone/upsert_claims.py`, figures derived from `papaya-context-source.md`). Each record is one canonical claim with a `stance` (`approved` | `banned`) and, for banned claims, the `correct_form` wording. The `search_claims` tool checks each draft claim: a match to an **approved** claim is strong `supported`; a match to or contradiction of a **banned** claim is `contradicted` + `banned_claim` (or `contradicts_approved_claim` when the draft disagrees with an approved figure). Positive *and* negative exemplars in one namespace — the gate knows both what Papaya *may* say and what it must *never* say. This is the demo's cleanest catch: draft says "EOR from $299" → `search_claims` returns the approved "$499" record → `contradicts_approved_claim`.
- **Notion** (I/O surface): the trigger reads the page; the delivery step writes back to it. This is where marketers already work — no dashboard, no JSON file, no vector-database literacy required of them.
- **Model calls:** OpenRouter `/chat/completions` via plain HTTP Request nodes — request and response stay readable on-canvas for a live demo; model swap is one field.
- **Build decision, one line:** the fact layer ships embedded; the published-content index lives in Pinecone because contradiction-checking needs semantic retrieval over a corpus too large to embed inline. Production reads the fact layer from the Part 2B git-backed store via a read-only MCP, and re-indexes the RAG on a "golden doc" webhook (see Production framing).

## 5. Output format & delivery

The loop closes where the marketer lives — inside the Notion page:

- **`Compliance Status`** property → set to `Ready` (all claims supported) or `Needs Review` (one or more not supported).
- **Audit comment** appended to the page: one line per flagged claim — the claim, its `risk_category`, `verdict`, the quoted evidence, and the source (context layer, or a named prior post + URL). Clean claims are summarized in a single "N claims checked, all supported" line so the comment stays scannable.
- **`verification_report`** (machine-readable, retained in the run for observability / Part 4): every claim's full verdict object + staleness flag + the per-stage cost/token/latency log.

No auto-publish. Ever. The agent categorizes and evidences; the human decides.

## 6. Human-in-the-loop checkpoints

The whole design *is* a checkpoint — the agent is the automated reviewer, the human is the decision-maker:

- **`Ready`** — all claims supported, context fresh. Human does a fast confirm-and-publish.
- **`Needs Review`** — the human opens the page to exactly the claims to check, with evidence and risk category already attached. Sub-minute triage, not a research session.
- **Standalone by construction** — the gate audits any draft, from any author (human, another tool, another AI). It never needed a generator; that's the point.

## 7. Guardrails

1. **No context, no verdict.** Empty/missing fact layer hard-stops the run.
2. **Facts fenced.** Only the two approved sources are admissible evidence; the verifier cannot "reason from general knowledge" that a claim is probably fine.
3. **Every non-supported claim escalates.** Any claim not `supported` → `Needs Review`. No "probably fine" exceptions.
4. **`banned_claims` checked explicitly**, every run.
5. **Freshness gate.** Fact layer older than 30 days forces `Needs Review` regardless of claim verdicts — degrade loudly.
6. **Empty-draft guard.** No body content → `needs_content`, never a silent pass.

## 8. Failure handling

- **Context missing →** exit before any model call.
- **Context stale →** run completes but is stamped and forced to review.
- **Pinecone/RAG unavailable →** the run does **not** fail; it completes on the fact layer alone and stamps the report `contradiction_check: skipped (RAG unavailable)`, forcing `Needs Review`. The published-content check is additive assurance, never a single point of failure for the whole gate.
- **Unparseable model output (JSON stages) →** salvage attempt (strip fences, find first `[`/`{`), then hard-fail naming the stage, rather than passing broken data downstream.
- **Notion write-back fails →** the verdict is still emitted to the run log and an alert fires; the audit is never silently lost.

## 9. Observability

Every stage logs model, input/output tokens, cost (OpenRouter extended accounting, with a hardcoded per-model rate as fallback), and latency, rolled into `verification_report.stages` + `total_cost_usd` + `total_tokens` per run. Same telemetry shape as the earlier build's real run logs — and the same evidence base for Part 4. A governance gate is cheap to run precisely because it's two LLM stages, not the five of a full generate-and-check pipeline; that cost delta is a Part 4 talking point.

**Two knowing tradeoffs of the native-agent build** (accepted deliberately; the HTTP+Code build is kept precisely to hedge them):
1. **No dry-run for the LLM stage.** The HTTP+Code build proves the whole pipeline's logic with `verify_governance_gate.js` and zero live API calls. A native AI Agent node can't be dry-run that way — the ReAct loop needs a real model. The deterministic nodes (entry guardrail, freshness, decision, Notion shaping) are still dry-runnable and the Pinecone RAG path is live-verified; the agent itself is tested **live** in n8n against the fixtures.
2. **Coarser observability.** The HTTP+Code build logged per-stage model/tokens/cost/latency (clean Part 4 evidence). The native agent reports token usage in its output metadata (sub-agent usage nested), which n8n surfaces in the execution view rather than as a clean per-stage row. The `Observability — Compile Run Report` node captures the verdict summary plus whatever usage the agent exposes; total run cost is read from the execution metadata. Less granular, accepted for the cleaner structure — a production tap would emit the nested usage to the telemetry store.

---

## Demo plan

Runs against a real Notion database (Noam's own), not a mock. Two beats:

1. **The catch it couldn't make before.** A draft page whose body states an EOR price that contradicts Papaya's live pricing post ($499/employee/month per the scraped `eor-pricing-cost` post). Move it to `Draft` → the gate fires → within seconds the page gets `Needs Review` and a comment: *"contradicts_prior_publication — draft says $X, papayaglobal.com/blog/eor-pricing-cost says $499/employee/month."* This is the new capability the published-content RAG unlocked; the old fact-only build literally could not catch it.
2. **The classics, in their new home.** The three real eval fixtures (clean / hallucinated / banned-claim) as actual Notion pages. Clean → `Ready`. Hallucinated and banned → `Needs Review` with the right `risk_category`. Proves the gate generalizes and that the taxonomy is doing real work.

Both beats end where a marketer would actually see them — a Notion comment — reinforcing "marketers never touch a vector database."

## Production framing

The demo tool is n8n: transparent, on-canvas, model-agnostic, fastest way to show the gate running live. **Production is a Claude Skill running the same two-stage gate on cheaper models**, wired to Notion webhooks — same context layer, same taxonomy, same two-source verification, packaged as portable git-tracked infra. Two production refinements to say out loud:
- **"Golden doc" auto-reindex:** when Product Marketing updates an approved source doc, a webhook re-indexes *that* document into the published-content RAG — the knowledge base stays current without anyone running a script or knowing what an embedding is.
- **Routing as Part 4 asks:** Haiku for extraction, Sonnet for the judgment call, deterministic code for routing — baked into the skill, not hand-picked per node.

Demo ≠ production; this is the two-layer answer.

---

## What I reused, what I cut, what I added (the receipts)

**Reused from the existing build (`papaya-marketing-agent.n8n.json`):** the fact context-layer loader, the deterministic presence + freshness guardrail, the claim-extraction and skeptical-verification prompts and their parse/salvage logic, both Pinecone namespaces, the observability compiler, the three real eval fixtures.

**Cut:** the entire Generator path (triage, grounded draft, brand-voice synthesis — 3 of 5 LLM stages), the Form Trigger, the bounded-retry loop (nothing to retry — no generator to bounce back to), the em-dash normalizer and markdown→HTML/WordPress output (no generation, no publishing). Roughly half the 36-node workflow.

**Added:** the Notion trigger + read, the published-content chunks as a *second* evidence source inside the verification prompt, the `risk_category` taxonomy field, and the Notion write-back (status property + audit comment).

**Net:** a leaner workflow (2 boxes, not 3) that does a narrower, higher-stakes job — and does the one thing the assignment rewards: real, testable, high-impact, defensible node by node.
