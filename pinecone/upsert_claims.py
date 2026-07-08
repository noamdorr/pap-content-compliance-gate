"""Seed the `claims` namespace of the papaya-kb Pinecone index with Papaya's
APPROVED (canonical, safe-to-repeat) and BANNED (do-not-write) claims.

Third source of truth for the Compliance Governance Gate: the search_claims RAG
tool checks each draft claim against this namespace -
  match to an approved claim  -> strong `supported`
  match/contradiction of a banned claim -> `contradicted` (+ banned_claim / contradicts_approved_claim)
  no match -> fall back to the docs + published namespaces.

Same integrated-embedding upsert pattern as upsert_pinecone.py (model
llama-text-embed-v2, field_map text -> chunk_text). Every figure is derived from
papaya-context-source.md (the authoritative Part 2B context layer) - none invented.

Run:
    PINECONE_API_KEY=... python upsert_claims.py
"""
from __future__ import annotations

import os
import sys
import time

from pinecone import Pinecone

INDEX_NAME = "papaya-kb"
NAMESPACE = "claims"
EMBED_MODEL = "llama-text-embed-v2"

# --- APPROVED: canonical claims that are safe to repeat (figures from papaya-context-source.md) ---
APPROVED = [
    ("eor-coverage",
     "Papaya Global provides Employer of Record (EOR) services in 180 countries, letting companies hire without a local entity while Papaya assumes 100% employment and statutory liability as the legal employer of record.",
     "coverage", "papayaglobal.com/llms.txt + /employer-of-record/"),
    ("payroll-coverage",
     "Papaya Global runs native, in-country payroll in 120+ countries, with direct legal entities in 100+ countries and payroll and compliance coverage across 180 countries.",
     "coverage", "papayaglobal.com/llms.txt"),
    ("pricing-eor",
     "Papaya Global Employer of Record (EOR) pricing starts at $499 per employee per month.",
     "pricing", "papayaglobal.com/pricing/"),
    ("pricing-cor",
     "Papaya Global Contractor of Record (COR) pricing starts at $295 per contractor per month.",
     "pricing", "papayaglobal.com/pricing/"),
    ("pricing-payroll-plus",
     "Papaya Global Payroll Plus pricing starts at $29 per employee per month.",
     "pricing", "papayaglobal.com/pricing/"),
    ("pricing-payments-os",
     "Papaya Global Payments OS pricing starts at $3.5 per transaction.",
     "pricing", "papayaglobal.com/pricing/"),
    ("founding",
     "Papaya Global was founded in 2016 by Eynat Guez, Ruben Drong, and Ofer Herman.",
     "company", "businesswire.com Series D release + Wikipedia"),
    ("funding",
     "Papaya Global raised a $250M Series D in September 2021, valuing the company at approximately $3.7B as of that round. No newer public valuation has been marked since, so the figure must be date-stamped.",
     "funding", "businesswire.com/news/home/20210913005227"),
    ("banco-wallet",
     "The Fireblocks-powered Banco workforce wallet, supporting both fiat and stablecoin payouts across 180+ countries, launched in January 2026.",
     "product", "prnewswire.com Banco/Fireblocks release, 2026-01-28"),
    ("payments-stats",
     "Papaya Global reports a 99.5% payment delivery rate, with about 90% of payments settled in real time (versus roughly 85% on traditional SWIFT).",
     "payments", "papayaglobal.com/llms.txt"),
    ("clients-proof",
     "Papaya Global serves 2,000+ global clients, approximately 35% of them Fortune 500, with 99% client retention and $50B+ processed annually across 180 countries.",
     "proof", "papayaglobal.com/llms.txt"),
    ("certifications",
     "Papaya Global holds SOC 1 Type 2, SOC 2 Type 2, ISO 27001, ISO 27701, and GDPR certifications.",
     "compliance", "papayaglobal.com/llms.txt + /security-and-privacy/"),
    ("banking-rails",
     "Papaya Global's payments run on Tier 1 banking rails (J.P. Morgan and Citi); its regulated payments arm, Azimo, is licensed across Tier-1 jurisdictions.",
     "payments", "papayaglobal.com/llms.txt"),
    ("papaya-one",
     "Papaya One reviews employment contracts across 95+ jurisdictions and 50 US states, with an average contract review under about 30 seconds; it launched in June 2026.",
     "product", "papayaglobal.com/papaya-one/"),
]

# --- BANNED: claims to block. `correct_form` gives the verifier the safe wording. ---
BANNED = [
    ("banned-real-time-settlement",
     "Do not claim Papaya offers 'real-time settlement' of payments.",
     "payments", "papaya-context-source.md banned_claims",
     "The verified claim is 'about 90% of payments settled in real time' - not blanket real-time settlement."),
    ("banned-stale-valuation",
     "Do not state any Papaya valuation as current, or any figure other than approximately $3.7B as of the September 2021 Series D.",
     "funding", "papaya-context-source.md banned_claims",
     "Use '~$3.7B as of the Sept 2021 Series D' and date-stamp it; no newer public mark exists."),
    ("banned-unlimited-liability",
     "Do not claim 'unlimited liability coverage' or use any uncapped-liability phrasing.",
     "liability", "papaya-context-source.md banned_claims",
     "The EOR termination guarantee is capped: $25K/$50K legal-defense tiers and one month base beyond statutory liability, across 22 covered countries."),
    ("banned-green-branding",
     "Do not describe Papaya as 'green' or use eco/green brand framing.",
     "brand", "papaya-context-source.md banned_claims",
     "Papaya's post-rebrand palette is red-coral (#FF3924) on deep navy (#081523); there is no green in the palette."),
    ("banned-customer-count-inflation",
     "Do not state customer counts beyond '2,000+ clients' or cite logos beyond Papaya's verified named list.",
     "proof", "papaya-context-source.md banned_claims",
     "Use '2,000+ clients' and only the named logos in the context layer."),
    ("banned-invented-superlatives",
     "Do not invent head-to-head superlatives such as 'the only', 'the first', or 'the largest' beyond Papaya's on-site competitor-comparison page or llms.txt.",
     "competitive", "papaya-context-source.md banned_claims",
     "Use only comparison claims that appear on Papaya's /competitor-comparison page or llms.txt."),
    ("banned-crypto-payroll",
     "Do not claim Papaya offers 'crypto payroll' broadly or that workers can be 'paid in Bitcoin'.",
     "payments", "papaya-context-source.md banned_claims",
     "The verified claim is that the Banco Wallet supports fiat AND stablecoin payouts (Fireblocks, Jan 2026); do not extrapolate."),
    ("banned-revenue-figures",
     "Do not state specific revenue, ARR, or profitability figures for Papaya.",
     "financials", "papaya-context-source.md banned_claims",
     "None are publicly verified; omit them."),
    ("banned-headcount-figures",
     "Do not state Papaya employee headcount figures.",
     "company", "papaya-context-source.md banned_claims",
     "Headcount is not verified in the context layer; omit it."),
    ("banned-unqualified-superlatives",
     "Do not state 'fastest' or 'cheapest' as independent fact.",
     "positioning", "papaya-context-source.md banned_claims",
     "Attribute Papaya's own qualifier: 'average go-live in weeks - the fastest in the industry' (per Papaya)."),
]


def build_records() -> list[dict]:
    records: list[dict] = []
    for _id, text, topic, source in APPROVED:
        records.append({
            "_id": f"claim-approved-{_id}",
            "chunk_text": text,
            "stance": "approved",
            "topic": topic,
            "source": source,
        })
    for _id, text, topic, source, correct in BANNED:
        records.append({
            "_id": f"claim-{_id}",
            "chunk_text": text,
            "stance": "banned",
            "topic": topic,
            "source": source,
            "correct_form": correct,
        })
    return records


def main() -> int:
    api_key = os.environ.get("PINECONE_API_KEY")
    if not api_key:
        print("ERROR: set PINECONE_API_KEY", file=sys.stderr)
        return 1

    pc = Pinecone(api_key=api_key)
    existing = {i["name"] for i in pc.list_indexes()}
    if INDEX_NAME not in existing:
        print(f"ERROR: index {INDEX_NAME} does not exist - run upsert_pinecone.py first.", file=sys.stderr)
        return 1

    index = pc.Index(INDEX_NAME)
    records = build_records()
    print(f"upserting {len(records)} claims ({len(APPROVED)} approved + {len(BANNED)} banned) "
          f"into namespace '{NAMESPACE}' ...", file=sys.stderr)

    for i in range(0, len(records), 32):
        sub = records[i:i + 32]
        for attempt in range(5):
            try:
                index.upsert_records(namespace=NAMESPACE, records=sub)
                break
            except Exception as e:
                wait = 2 ** attempt
                print(f"  retry after {wait}s: {str(e)[:200]}", file=sys.stderr)
                time.sleep(wait)

    print("done. waiting for the index to reflect the upsert ...", file=sys.stderr)
    time.sleep(6)
    stats = index.describe_index_stats()
    ns = stats.get("namespaces", {}).get(NAMESPACE, {})
    print(f"namespace '{NAMESPACE}' vector_count: {ns.get('vector_count')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
