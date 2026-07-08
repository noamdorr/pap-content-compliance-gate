#!/usr/bin/env python3
"""Builds papaya-governance-gate-agent.n8n.json - the NATIVE-AGENT rebuild of the
Papaya Compliance Governance Gate.

Same locked logic as the HTTP+Code build (papaya-governance-gate.n8n.json, kept as
the dry-run-verified backup) but restructured around n8n's native AI Agent node:

  BOX 1  PARENT / ORCHESTRATOR (deterministic, no LLM)
    Notion trigger -> entry guardrail -> filter (skip non-reviewable)
    -> get draft body -> assemble draft + embed context layer + freshness
    -> IF (empty draft bypasses the agent)

  BOX 2  COMPLIANCE GOVERNANCE AGENT (native)
    @n8n/n8n-nodes-langchain.agent (Sonnet) that EXTRACTS every claim and VERIFIES
    each, with a Structured Output Parser enforcing the per-claim verdict schema, and
    a kb_agent sub-agent (Haiku, separate context window) exposing three Pinecone
    integrated-embedding RAG tools: search_facts / search_published / search_claims.

  BOX 3  DECISION / DELIVERY / OBSERVABILITY (deterministic, no LLM)
    Parent decision (ship/hold rule) -> Notion write-back (status + audit block)
    -> observability compiler.

Node types / typeVersions / params confirmed against the n8n MCP get_node_types
(read-only) on 2026-07-08. The agent + sub-node connection shapes (ai_languageModel,
ai_tool, ai_outputParser) follow a previously verified sub-agent wiring pattern.

NOT created/pushed via the n8n MCP - this emits a JSON file for manual import.
"""
import json
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONTEXT_SOURCE = HERE.parent / "papaya-context-source.md"
PINECONE_HOST = "https://papaya-kb-a15xv56.svc.aped-4627-b74a.pinecone.io"

nodes, connections, NAME_SET = [], {}, set()


def nid():
    return str(uuid.uuid4())


def add_node(name, ntype, version, params, pos, notes=None, extra=None):
    assert name not in NAME_SET, f"duplicate node name: {name}"
    NAME_SET.add(name)
    node = {"id": nid(), "name": name, "type": ntype, "typeVersion": version,
            "position": pos, "parameters": params}
    if notes:
        node["notes"] = notes
        node["notesInFlow"] = False
    if extra:
        node.update(extra)
    nodes.append(node)
    return name


def connect(source, target, source_output=0, target_input=0):
    """main-type connection (data flow)."""
    conns = connections.setdefault(source, {}).setdefault("main", [])
    while len(conns) <= source_output:
        conns.append([])
    conns[source_output].append({"node": target, "type": "main", "index": target_input})


def connect_ai(source, target, conn_type, target_input=0):
    """ai_* connection (model / tool / outputParser). Source node points at the
    consumer, mirroring n8n's export shape."""
    conns = connections.setdefault(source, {}).setdefault(conn_type, [])
    while len(conns) <= 0:
        conns.append([])
    conns[0].append({"node": target, "type": conn_type, "index": target_input})


# ===========================================================================
# Embedded context layer (source of truth #1) - read from the authoritative
# Part 2B artifact so the import is self-contained and never drifts from source.
# ===========================================================================
CONTEXT_LAYER = CONTEXT_SOURCE.read_text(encoding="utf-8")

# ===========================================================================
# BOX 1 - PARENT / ORCHESTRATOR
# ===========================================================================

add_node(
    "Notion Trigger - Page Updated in Content DB",
    "n8n-nodes-base.notionTrigger", 1,
    {
        "event": "pagedUpdatedInDatabase",  # exact n8n enum (note the 'paged' typo in n8n itself)
        "databaseId": {"__rl": True, "mode": "id", "value": "SET_DATABASE_ID_AT_IMPORT"},
        "simple": False,  # raw Notion page object -> stable properties.Status.status.name
        "pollTimes": {"item": [{"mode": "everyMinute"}]},
    },
    [-380, 0],
    notes=("Polls the content-calendar DB for updated pages. For the live demo, click 'fetch test "
           "event' to pull the latest updated page instantly. Production swaps this for Notion's "
           "native automation -> n8n Webhook (zero latency). Set the databaseId + Notion credential "
           "at import. simple=false so the raw page object (properties.Status...) is what the guardrail reads."),
)

add_node(
    "Guardrail - Entry Conditions",
    "n8n-nodes-base.code", 2,
    {"mode": "runOnceForAllItems", "language": "javaScript", "jsCode":
        "const p = $input.first().json;\n"
        "// Raw Notion page object: id + properties. Status may be a 'status'- or 'select'-type property.\n"
        "const props = p.properties || {};\n"
        "const statusProp = props.Status || props.status || {};\n"
        "const status = (statusProp.status && statusProp.status.name) || (statusProp.select && statusProp.select.name) || '';\n"
        "const titleArr = (props.Name && props.Name.title) || (props.Title && props.Title.title) || [];\n"
        "const page_title = titleArr.map(t => t.plain_text).join('') || '(untitled)';\n"
        "const page_id = p.id;\n"
        "// Only compliance-review states proceed. Anything else is a no-op skip (not an error).\n"
        "const reviewable = (status === 'Draft' || status === 'In Review');\n"
        "return [{ json: { page_id, page_title, status, _skip: !reviewable, skip_reason: reviewable ? '' : 'status_not_reviewable' } }];"},
    [-160, 0],
    notes=("Deterministic. Fires on any page update; proceeds only when Status is Draft/In Review. "
           "Extracts page_id/title/status from the raw Notion page and flags _skip for anything else."),
)

add_node(
    "Only Draft / In Review",
    "n8n-nodes-base.filter", 2.3,
    {"conditions": {
        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
        "combinator": "and",
        "conditions": [{
            "id": nid(),
            "leftValue": "={{ $json._skip }}",
            "rightValue": False,
            "operator": {"type": "boolean", "operation": "false", "singleValue": True},
        }],
    }},
    [60, 0],
    notes=("Deterministic no-op skip: drops any page whose Status is not Draft/In Review so the gate "
           "never touches a page it wasn't asked to review, and never spends a model call on one."),
)

add_node(
    "Get Draft Body (Notion)",
    "n8n-nodes-base.notion", 2.2,
    {
        "resource": "block", "operation": "getAll",
        "blockId": {"__rl": True, "mode": "id", "value": "={{ $json.page_id }}"},
        "returnAll": True,
    },
    [280, 0],
    notes="Pulls the page's block children (the actual draft text). Same Notion credential as the trigger.",
)

add_node(
    "Assemble Draft + Load Context Layer",
    "n8n-nodes-base.code", 2,
    {"mode": "runOnceForAllItems", "language": "javaScript", "jsCode":
        "const CONTEXT_LAYER = " + json.dumps(CONTEXT_LAYER) + ";\n\n"
        "// --- assemble draft text from Notion blocks (defensive across raw + n8n-simplified shapes) ---\n"
        "const meta = $('Guardrail - Entry Conditions').first().json;\n"
        "const blocks = $input.all().map(i => i.json);\n"
        "function blockText(b){\n"
        "  const inner = (b && b[b.type]) || {};\n"
        "  const rt = inner.rich_text || inner.text || [];\n"
        "  if (Array.isArray(rt) && rt.length) return rt.map(x => (x.plain_text || (x.text && x.text.content) || '')).join('');\n"
        "  if (typeof b.content === 'string') return b.content;      // n8n simplified\n"
        "  if (typeof inner.content === 'string') return inner.content;\n"
        "  return '';\n"
        "}\n"
        "const draft_text = blocks.map(blockText).filter(Boolean).join('\\n\\n').trim();\n\n"
        "// Guardrail: no context, no verdict.\n"
        "if (!CONTEXT_LAYER || CONTEXT_LAYER.length < 500) { throw new Error('GUARDRAIL: context layer missing/empty. No context, no verdict.'); }\n"
        "// Guardrail: empty draft short-circuits to needs_content (never a fake pass). IF node routes it around the agent.\n"
        "if (draft_text.length < 40) { return [{ json: { ...meta, draft_text, context: CONTEXT_LAYER, _needs_content: true } }]; }\n\n"
        "// Freshness: read the last_verified / compiled stamp out of the context layer meta.\n"
        "const m = CONTEXT_LAYER.match(/last_verified[:=]\\s*(\\d{4}-\\d{2}-\\d{2})/i) || CONTEXT_LAYER.match(/compiled[:=]\\s*(\\d{4}-\\d{2}-\\d{2})/i);\n"
        "const stampedDate = m ? m[1] : '2026-07-06';\n"
        "const ageDays = Math.floor((Date.now() - new Date(stampedDate + 'T00:00:00Z').getTime()) / 86400000);\n"
        "return [{ json: { ...meta, draft_text, context: CONTEXT_LAYER, _needs_content: false,\n"
        "  context_meta_last_verified: stampedDate, context_stale: ageDays > 30, context_age_days: ageDays } }];"},
    [500, 0],
    notes=("Concatenates the Notion block text into draft_text, embeds the Papaya context layer "
           "(source of truth #1), and stamps context freshness. Empty context -> hard stop. Empty "
           "draft -> _needs_content. Block parser is defensive across raw Notion + n8n-simplified shapes."),
)

add_node(
    "Empty draft? (needs content)",
    "n8n-nodes-base.if", 2.3,
    {"conditions": {
        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
        "combinator": "and",
        "conditions": [{
            "id": nid(),
            "leftValue": "={{ $json._needs_content }}",
            "rightValue": True,
            "operator": {"type": "boolean", "operation": "true", "singleValue": True},
        }],
    }},
    [740, 0],
    notes=("TRUE (empty draft) -> straight to the decision node as 'Needs Content', never a model call "
           "on nothing. FALSE (real draft) -> the Compliance Governance Agent."),
)

connect("Notion Trigger - Page Updated in Content DB", "Guardrail - Entry Conditions")
connect("Guardrail - Entry Conditions", "Only Draft / In Review")
connect("Only Draft / In Review", "Get Draft Body (Notion)")
connect("Get Draft Body (Notion)", "Assemble Draft + Load Context Layer")
connect("Assemble Draft + Load Context Layer", "Empty draft? (needs content)")


# ===========================================================================
# BOX 2 - COMPLIANCE GOVERNANCE AGENT (native)
# ===========================================================================

AGENT_SYSTEM = (
    "You are a skeptical compliance reviewer for Papaya Global, a payroll/EOR company that legally owns "
    "liability across 160+ jurisdictions. You do not write or improve copy. You audit a draft before it "
    "can be published under Papaya's name and decide, for every checkable claim, whether it is provable "
    "from approved evidence - and if not, what KIND of compliance risk it is.\n\n"
    "YOUR JOB, in order:\n"
    "1. EXTRACT every checkable factual claim in the draft: numbers, dates, prices, product capabilities, "
    "company facts (founders, funding, customers, certifications, partnerships), superlatives (\"the only\", "
    "\"the first\", \"the largest\"), named third parties, and any jurisdiction/compliance or liability "
    "assertion. Be over-inclusive - a missed claim is an unchecked claim. Leave out pure opinion and generic "
    "industry statements (\"payroll is complicated\").\n"
    "2. GATHER EVIDENCE for each claim from TWO kinds of source only:\n"
    "   - SOURCE #1, the FACT CONTEXT LAYER given to you in the user message (primary ground truth). If a "
    "fact is not in this layer and the knowledge base does not back it, it is NOT admissible.\n"
    "   - The knowledge base, via your kb_agent tool. Call kb_agent for any claim you cannot fully settle "
    "from the context layer, and ALWAYS call it to (a) check whether the claim CONTRADICTS one of Papaya's "
    "already-published posts and (b) check the claim against Papaya's approved and banned canonical claims. "
    "Pass kb_agent one specific claim/question at a time; it returns whether the KB supports, contradicts, "
    "or is silent, with a quoted source.\n"
    "3. RETURN one verdict object per claim.\n\n"
    "VERDICT RULES:\n"
    "- A claim is \"supported\" ONLY if a source explicitly backs it. Never reason from general knowledge or "
    "plausibility.\n"
    "- verdict: \"supported\" | \"contradicted\" | \"unverifiable\".\n"
    "- source: \"context_layer\" | \"prior_publication\" | \"approved_claim\" | \"none\".\n"
    "- evidence: a verbatim quote from whichever source backs or breaks the claim (empty string if none).\n"
    "- confidence: a number 0..1.\n"
    "- risk_category (use \"none\" only when supported):\n"
    "   - hallucinated_fact - a stat/feature not present in any approved source.\n"
    "   - stale_figure - a figure the layer marks as dated (e.g. the $3.7B / Sept-2021 valuation stated as current).\n"
    "   - banned_claim - matches the context layer's banned_claims list or a banned canonical claim (e.g. "
    "\"real-time settlement\" vs the real \"about 90% settled in real time\"; \"green\" branding; customer "
    "counts beyond the approved figure).\n"
    "   - contradicts_prior_publication - disagrees with one of Papaya's own published posts (kb_agent "
    "search_published). Quote the prior post in evidence.\n"
    "   - contradicts_approved_claim - disagrees with an approved canonical claim (kb_agent search_claims).\n"
    "   - jurisdiction_overreach - claims coverage/compliance in a country the layer does not support.\n"
    "   - liability_language - uncapped / \"unlimited\" liability phrasing (Papaya caps its liability).\n"
    "- Check the banned_claims list explicitly on every run.\n"
    "You NEVER decide publish vs hold - a deterministic node downstream does that. You only categorize and evidence."
)

AGENT_TEXT = (
    "=DRAFT SUBMITTED FOR COMPLIANCE REVIEW\n"
    "Title: {{ $json.page_title }}\n\n"
    "<draft>\n"
    "{{ $json.draft_text }}\n"
    "</draft>\n\n"
    "=== SOURCE #1: PAPAYA FACT CONTEXT LAYER (primary ground truth; if a fact is not here and the "
    "knowledge base does not back it, it is not admissible) ===\n"
    "{{ $json.context }}\n\n"
    "Extract every checkable claim from the draft above. Verify each against this fact context layer and "
    "- via your kb_agent tool - against Papaya's published posts and approved/banned canonical claims. "
    "Return exactly one verdict object per claim, in the required schema."
)

add_node(
    "Compliance Governance Agent",
    "@n8n/n8n-nodes-langchain.agent", 3.1,
    {
        "promptType": "define",
        "text": AGENT_TEXT,
        "hasOutputParser": True,
        "options": {"systemMessage": AGENT_SYSTEM},
    },
    [1000, 0],
    notes=("Native AI Agent (Sonnet - the judgment call). Extracts every claim AND verifies each in one "
           "ReAct loop, calling the kb_agent tool for KB evidence. The Structured Output Parser enforces the "
           "per-claim verdict schema. No json_object response-format and no fallback model: forcing json "
           "breaks tool-calling, and a wrong 'supported' verdict is the worst failure in the system - so the "
           "judgment model is Sonnet or it fails loud, never a cheaper stand-in."),
)

add_node(
    "Sonnet (judgment)",
    "@n8n/n8n-nodes-langchain.lmChatOpenRouter", 1,
    {"model": "anthropic/claude-sonnet-5", "options": {"temperature": 0.1, "maxTokens": 4000}},
    [880, 240],
    notes="OpenRouter -> anthropic/claude-sonnet-5. Model swap is one field. Set an OpenRouter credential at import.",
)

VERDICT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "ComplianceGateVerdicts",
    "type": "object",
    "required": ["verdicts"],
    "additionalProperties": False,
    "properties": {
        "verdicts": {
            "type": "array",
            "description": "One object per checkable claim found in the draft.",
            "items": {
                "type": "object",
                "required": ["id", "claim", "verdict", "evidence", "source", "confidence", "risk_category"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": ["integer", "string"]},
                    "claim": {"type": "string", "description": "The claim, copied through unchanged."},
                    "verdict": {"type": "string", "enum": ["supported", "contradicted", "unverifiable"]},
                    "evidence": {"type": "string", "description": "Verbatim quote from the backing/breaking source, or empty string."},
                    "source": {"type": "string", "enum": ["context_layer", "prior_publication", "approved_claim", "none"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "risk_category": {"type": "string", "enum": [
                        "none", "hallucinated_fact", "stale_figure", "banned_claim",
                        "contradicts_prior_publication", "contradicts_approved_claim",
                        "jurisdiction_overreach", "liability_language"]},
                },
            },
        }
    },
}

add_node(
    "Verdict Schema",
    "@n8n/n8n-nodes-langchain.outputParserStructured", 1.3,
    {"schemaType": "manual", "inputSchema": json.dumps(VERDICT_SCHEMA, indent=2), "autoFix": False},
    [1120, 240],
    notes=("Enforces the per-claim verdict schema (id, claim, verdict, evidence, source, confidence, "
           "risk_category). autoFix off: malformed output should fail loud rather than be silently "
           "'repaired' and passed downstream (DESIGN failure-handling)."),
)

# --- kb_agent sub-agent (Haiku) + three Pinecone RAG tools ---
KB_SYSTEM = (
    "You are Papaya Global's knowledge-base researcher. A parent compliance-review agent sends you one claim "
    "or question at a time; your job is to search Papaya's knowledge base and report whether it SUPPORTS, "
    "CONTRADICTS, or is SILENT on that claim - with a verbatim quote and its source. You do not judge the "
    "whole draft; you return grounded evidence the parent can use directly.\n\n"
    "You have three retrieval tools:\n"
    "- search_facts - Papaya's verified fact layer (company facts, products, pricing, proof points, "
    "certifications, banned_claims). Use to confirm or deny a factual claim.\n"
    "- search_published - Papaya's own recent published blog posts. Use to check whether the claim "
    "CONTRADICTS something Papaya already published (e.g. a price that disagrees with the live pricing post). "
    "This is the \"the old post says X, the new draft says Y\" check.\n"
    "- search_claims - Papaya's approved (safe-to-repeat, canonical) and banned (do-not-write) claims. A "
    "match to an approved claim is strong support; a match to or violation of a banned claim is a "
    "contradiction - report its stance and any correct_form.\n\n"
    "HOW TO WORK:\n"
    "- Query the tool(s) most relevant to the claim; for a factual/price/coverage claim, check search_facts "
    "AND search_published (contradiction) AND search_claims (approved/banned). Use the claim's own wording as "
    "the query text.\n"
    "- Only state what the KB returns. Do not invent facts, prices, dates, or URLs. If a tool returns nothing "
    "or errors, treat the KB as SILENT / unavailable on that point - never guess.\n\n"
    "RETURN a short, direct answer (no preamble):\n"
    "- Verdict: SUPPORTED | CONTRADICTED | SILENT\n"
    "- Evidence: the verbatim quote (and stance, for a claims match)\n"
    "- Source: which namespace + the title/url/slug or stance\n"
    "- Confidence: high / medium / low\n"
    "Keep it under ~150 words; the parent is assembling a per-claim verdict, not reading the whole KB."
)

add_node(
    "kb_agent",
    "@n8n/n8n-nodes-langchain.agentTool", 3,
    {
        "toolDescription": ("Papaya knowledge-base researcher (sub-agent). Ask it about ONE claim/question at "
                            "a time; it searches Papaya's fact layer, published posts, and approved/banned "
                            "canonical claims and returns whether the KB supports, contradicts, or is silent, "
                            "with a quoted source. Use it to verify any claim you cannot settle from the "
                            "context layer, and always to check contradictions with published posts and the "
                            "approved/banned claims list."),
        "text": "={{ /*n8n-auto-generated-fromAI-override*/ $fromAI('query', 'the specific claim or question to research against the Papaya knowledge base', 'string') }}",
        "options": {"systemMessage": KB_SYSTEM},
    },
    [1040, 420],
    notes=("Sub-agent with its OWN context window (Haiku) so raw KB text never pollutes the main agent's "
           "reasoning. Owns the three Pinecone RAG tools."),
)

add_node(
    "Haiku (kb researcher)",
    "@n8n/n8n-nodes-langchain.lmChatOpenRouter", 1,
    {"model": "anthropic/claude-haiku-4.5", "options": {"temperature": 0.1, "maxTokens": 1200}},
    [820, 640],
    notes="OpenRouter -> anthropic/claude-haiku-4.5. The cheap model does the mechanical retrieval/synthesis. Swappable.",
)


def rag_tool(name, namespace, description, fields, pos):
    body = (
        "={\n"
        "  \"query\": {\n"
        "    \"top_k\": 5,\n"
        "    \"inputs\": {\n"
        "      \"text\": \"{{ $fromAI('query', 'the claim or query text to search', 'string') }}\"\n"
        "    }\n"
        "  },\n"
        "  \"fields\": " + json.dumps(fields) + "\n"
        "}"
    )
    add_node(
        name,
        "n8n-nodes-base.httpRequestTool", 4.4,
        {
            "toolDescription": description,
            "method": "POST",
            "url": f"{PINECONE_HOST}/records/namespaces/{namespace}/search",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "pineconeApi",
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": body,
            "options": {},
        },
        pos,
        notes=(f"Pinecone integrated-embedding search over namespace '{namespace}'. top_k is snake_case "
               "(topK fails with a deserialize error). Set a Pinecone (Api-Key header) credential at import."),
    )


rag_tool(
    "search_facts", "docs",
    ("Search Papaya's verified FACT LAYER (company facts, products, pricing, proof points, certifications, "
     "banned_claims). Returns the top matching fact chunks. Use to confirm or deny a factual claim."),
    ["chunk_text", "title", "section", "doc"], [1000, 640],
)
rag_tool(
    "search_published", "published",
    ("Search Papaya's OWN recently PUBLISHED blog posts. Returns the top matching chunks with title + url. "
     "Use to check whether a draft claim contradicts what Papaya already published."),
    ["chunk_text", "title", "url", "slug"], [1180, 640],
)
rag_tool(
    "search_claims", "claims",
    ("Search Papaya's APPROVED and BANNED canonical claims. Returns matching claims with their stance "
     "(approved|banned) and any correct_form. Use to check whether a draft claim matches an approved claim "
     "(support) or a banned claim (contradiction)."),
    ["chunk_text", "stance", "topic", "source", "correct_form"], [1360, 640],
)

# AI wiring (source -> consumer)
connect_ai("Sonnet (judgment)", "Compliance Governance Agent", "ai_languageModel")
connect_ai("Verdict Schema", "Compliance Governance Agent", "ai_outputParser")
connect_ai("kb_agent", "Compliance Governance Agent", "ai_tool")
connect_ai("Haiku (kb researcher)", "kb_agent", "ai_languageModel")
connect_ai("search_facts", "kb_agent", "ai_tool")
connect_ai("search_published", "kb_agent", "ai_tool")
connect_ai("search_claims", "kb_agent", "ai_tool")

# IF false (real draft) -> agent ; agent -> decision (below)
connect("Empty draft? (needs content)", "Compliance Governance Agent", source_output=1)


# ===========================================================================
# BOX 3 - DECISION / DELIVERY / OBSERVABILITY
# ===========================================================================

add_node(
    "Parent - Decide Ready / Needs Review",
    "n8n-nodes-base.code", 2,
    {"mode": "runOnceForAllItems", "language": "javaScript", "jsCode":
        "// Deterministic. The LLM never decides publish vs hold.\n"
        "const base = $('Assemble Draft + Load Context Layer').first().json;\n"
        "if (base._needs_content) {\n"
        "  return [{ json: { page_id: base.page_id, page_title: base.page_title, compliance_status: 'Needs Content',\n"
        "    audit_comment: 'This page has no draft body to review yet.', claims_checked: 0, unsupported: [], verdicts: [],\n"
        "    context_stale: !!base.context_stale, context_age_days: base.context_age_days } }];\n"
        "}\n"
        "// Agent output lands in $json.output (parsed by the Structured Output Parser). Defensive read.\n"
        "const agentJson = $('Compliance Governance Agent').first().json;\n"
        "const parsed = (agentJson && agentJson.output) || agentJson || {};\n"
        "const verdicts = Array.isArray(parsed) ? parsed : (parsed.verdicts || []);\n"
        "const unsupported = verdicts.filter(v => v && v.verdict !== 'supported');\n"
        "const stale = !!base.context_stale;\n"
        "// Any non-supported claim OR stale context forces Needs Review.\n"
        "const status = (unsupported.length > 0 || stale) ? 'Needs Review' : 'Ready';\n"
        "// Build a scannable Notion annotation (ASCII hyphens only - anti-AI-tell discipline).\n"
        "const srcMap = { prior_publication: 'prior published post', context_layer: 'fact layer', approved_claim: 'approved-claims list', none: 'no source' };\n"
        "const lines = [];\n"
        "lines.push(`Compliance gate: ${status}  (${verdicts.length} claims checked)`);\n"
        "if (stale) lines.push(`- WARNING context layer is ${base.context_age_days} days old (>30) - forced to review.`);\n"
        "for (const v of unsupported) {\n"
        "  const ev = (v.evidence || '').slice(0, 240);\n"
        "  const src = srcMap[v.source] || v.source || 'no source';\n"
        "  lines.push(`- [${v.risk_category || 'unverified'}] ${v.verdict}: \"${v.claim}\"  |  ${src}: ${ev}`);\n"
        "}\n"
        "if (!unsupported.length && !stale) lines.push('- All claims trace to an approved source. Cleared for a human to publish.');\n"
        "const audit_comment = lines.join('\\n');\n"
        "return [{ json: { page_id: base.page_id, page_title: base.page_title, compliance_status: status,\n"
        "  audit_comment, claims_checked: verdicts.length, unsupported, verdicts,\n"
        "  context_stale: stale, context_age_days: base.context_age_days } }];"},
    [1420, 0],
    notes=("Deterministic ship/hold rule: _needs_content -> Needs Content; any claim not 'supported' -> Needs "
           "Review; stale context -> Needs Review; else Ready. Emits the categorized Notion annotation "
           "(quoted evidence + risk_category per flag). This is the governance guarantee - the LLM never decides."),
)

# Both the agent path and the empty-draft bypass converge here.
connect("Compliance Governance Agent", "Parent - Decide Ready / Needs Review")
connect("Empty draft? (needs content)", "Parent - Decide Ready / Needs Review", source_output=0)

DEC = "Parent - Decide Ready / Needs Review"

add_node(
    "Deliver - Update Compliance Status (Notion)",
    "n8n-nodes-base.notion", 2.2,
    {
        "resource": "databasePage", "operation": "update",
        "pageId": {"__rl": True, "mode": "id", "value": f"={{{{ $('{DEC}').first().json.page_id }}}}"},
        "simple": True,
        "propertiesUi": {"propertyValues": [
            {"key": "Compliance Status|select", "selectValue": f"={{{{ $('{DEC}').first().json.compliance_status }}}}"}
        ]},
    },
    [1640, 0],
    notes=("Writes the verdict to a 'Compliance Status' SELECT property on the page. Create that property "
           "(options: Ready / Needs Review / Needs Content) on the demo DB and confirm the name at import. "
           "Reads page_id + status by back-reference to the decision node (the Notion node replaces $json "
           "with its API response, so downstream nodes must not rely on passthrough)."),
)

add_node(
    "Deliver - Append Audit Block (Notion)",
    "n8n-nodes-base.notion", 2.2,
    {
        "resource": "block", "operation": "append",
        "blockId": {"__rl": True, "mode": "id", "value": f"={{{{ $('{DEC}').first().json.page_id }}}}"},
        "blockUi": {"blockValues": [
            {"type": "paragraph", "richText": False, "textContent": f"={{{{ $('{DEC}').first().json.audit_comment }}}}"}
        ]},
    },
    [1860, 0],
    notes=("Appends the categorized audit as a paragraph block in the page body - quoted evidence + "
           "risk_category per flag. The n8n Notion node has no comment resource; a true threaded comment is a "
           "one-node production swap (HTTP Request -> Notion POST /v1/comments). No auto-publish; the human decides."),
)

add_node(
    "Observability - Compile Run Report",
    "n8n-nodes-base.code", 2,
    {"mode": "runOnceForAllItems", "language": "javaScript", "jsCode":
        "const dec = $('" + DEC + "').first().json;\n"
        "// Native agent: per-stage token/cost is coarser than the HTTP+Code build (accepted tradeoff).\n"
        "// Try to surface any usage the agent exposes; total run usage otherwise lives in n8n's execution metadata.\n"
        "let token_usage = null;\n"
        "try { const a = $('Compliance Governance Agent').first().json; token_usage = (a && (a.tokenUsage || (a.output && a.output.usage))) || null; } catch (e) {}\n"
        "const verification_report = {\n"
        "  compliance_status: dec.compliance_status,\n"
        "  claims_checked: dec.claims_checked || 0,\n"
        "  unsupported: dec.unsupported || [],\n"
        "  verdicts: dec.verdicts || [],\n"
        "  context_stale: !!dec.context_stale,\n"
        "  context_age_days: dec.context_age_days,\n"
        "  token_usage,\n"
        "  observability_note: 'Native AI Agent: per-stage token/cost is coarser than the HTTP+Code backup. Total run usage is in the n8n execution metadata; a production tap would emit it to the telemetry store.',\n"
        "};\n"
        "return [{ json: { page_id: dec.page_id, page_title: dec.page_title, compliance_status: dec.compliance_status, audit_comment: dec.audit_comment, verification_report } }];"},
    [2080, 0],
    notes=("Rolls up the verdict summary + any token usage the native agent exposes. Coarser than the "
           "HTTP+Code build's per-stage logs - the accepted cost of the native-agent structure (DESIGN observability)."),
)

connect(DEC, "Deliver - Update Compliance Status (Notion)")
connect("Deliver - Update Compliance Status (Notion)", "Deliver - Append Audit Block (Notion)")
connect("Deliver - Append Audit Block (Notion)", "Observability - Compile Run Report")


# ===========================================================================
# STICKY NOTES
# ===========================================================================

add_node(
    "READ ME - Papaya Compliance Governance Gate",
    "n8n-nodes-base.stickyNote", 1,
    {"width": 480, "height": 400, "content": (
        "## Papaya Compliance Governance Gate  (native-agent build)\n\n"
        "**Built by Noam Dorr - Papaya Global AI GTM Engineer home assignment, Part 3.**\n\n"
        "Agent-in-the-loop, not human-in-the-loop: a marketer writes, the agent audits. Fires when a Notion "
        "page moves to Draft, extracts every claim, verifies each against THREE sources of truth (the fact "
        "context layer + Papaya's own published posts + Papaya's approved/banned canonical claims, via a "
        "Pinecone RAG sub-agent), tags each flag with a compliance risk_category, and writes the verdict back "
        "to the page. It never publishes.\n\n"
        "**Structure:** a native AI Agent (Sonnet) that extracts + verifies, with a kb_agent sub-agent (Haiku) "
        "owning three Pinecone RAG tools, wrapped by deterministic guardrails, a deterministic ship/hold "
        "decision, and Notion write-back. The LLM never decides publish vs hold.\n\n"
        "**Before running, set four credentials + the databaseId:**\n"
        "1. Notion (trigger + both Deliver nodes) and the trigger's databaseId.\n"
        "2. OpenRouter (on 'Sonnet (judgment)' and 'Haiku (kb researcher)').\n"
        "3. Pinecone Api-Key (on all three search_* RAG tools).\n"
        "4. On the demo DB, add a 'Compliance Status' select property with options Ready / Needs Review / Needs Content.\n\n"
        "**Demo:** move a page to Draft, click 'fetch test event' on the trigger, execute. The planted "
        "contradiction fixture (EOR price disagreeing with the live pricing post) flips the page to Needs "
        "Review with a categorized annotation.\n\n"
        "The HTTP+Code build (papaya-governance-gate.n8n.json) is the dry-run-verified backup."
    )},
    [-400, -300],
)


def group_label(title, body, pos, w, h):
    add_node(f"Group - {title}", "n8n-nodes-base.stickyNote", 1,
             {"width": w, "height": h, "content": f"### {title}\n{body}"}, pos)


group_label("PARENT / ORCHESTRATOR",
            "Deterministic: trigger, entry guardrail, no-op skip filter, context load + freshness, empty-draft "
            "routing. No LLM.",
            [-400, -140], 1300, 130)
group_label("COMPLIANCE GOVERNANCE AGENT (native, Sonnet)",
            "Extracts every claim AND verifies each in one ReAct loop. Structured Output Parser enforces the "
            "per-claim verdict schema.",
            [860, 150], 400, 60)
group_label("KB RESEARCHER SUB-AGENT + RAG (Haiku)",
            "Separate context window. Three Pinecone integrated-embedding tools: search_facts (docs) / "
            "search_published / search_claims.",
            [800, 340], 720, 60)
group_label("DECISION / DELIVERY / OBSERVABILITY",
            "Deterministic ship/hold decision -> Notion write-back (status + audit block) -> run report. The "
            "LLM never decides publish vs hold.",
            [1400, -140], 880, 130)


# ===========================================================================
def main():
    wf = {"name": "Papaya Compliance Governance Gate - Native Agent",
          "nodes": nodes, "connections": connections, "active": False,
          "settings": {"executionOrder": "v1"}, "pinData": {}}
    out = HERE / "papaya-governance-gate-agent.n8n.json"
    out.write_text(json.dumps(wf, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {out.name} ({len(nodes)} nodes, {len(connections)} source-nodes with connections)")


if __name__ == "__main__":
    main()
