# Bird-card catalogue review — 2026-07-25

## Publication identity

- Catalogue version: `2026-07-25.2`
- Species reviewed: 40
- Required factual fields reviewed: 360
- Catalogue SHA-256: `ff46b01a07358e86406f7e765bd1dc927ec97d5994a653867a3a7ac164916029`
- Retained Cornell evidence SHA-256: `1ad859b0518ef32d1888c041d40d55694e0cf9f6a30747442e3263d5d479300e`
- Final unresolved conflicts: 0

The review manifest records provenance for the exact catalogue bytes. Model names and verdicts are not cryptographic authentication or a substitute for source evidence.

## Evidence process

1. A primary research agent built a 40-species, field-level source corpus.
2. A separate research agent independently rebuilt the evidence matrix and flagged conflicts rather than reading the first agent's answer.
3. OpenAI GPT-5.5 Pro and xAI Grok 4.5 independently reviewed all 40 species and 360 required factual fields.
4. GPT-5.5 Pro passed the first catalogue. Grok required changes for unsupported/narrower measurement ranges, implicit source-taxonomy synonyms, and several controlled labels.
5. Because the live Cornell pages were Cloudflare-blocked, the host retrieved the latest real (non-challenge) Internet Archive captures of ten Cornell measurement pages. Exact capture timestamp, CDX digest, snapshot URL and quoted measurement block are retained in `bird_cards.evidence.json`.
6. Retained source text—not model consensus—controlled the reconciliation. It corrected values that GPT-5.5 Pro had accepted from bare source links.
7. Both models then received the complete revised catalogue, retained evidence, both first-round verdicts, the semantic delta, and the new hash. Both returned `passed` with zero findings against the same revised hash.

## Material reconciliations

- Scarlet Tanager: length `16–17 cm`; wingspan `25–29 cm`.
- Song Sparrow: length `12–17 cm`; wingspan `18–24 cm`.
- Eastern Phoebe: length `14–17 cm`; wingspan `26–28 cm`.
- American Redstart wingspan: `16–19 cm`.
- Barn Swallow length: `15–19 cm`.
- White-breasted Nuthatch length: `13–14 cm`.
- All twenty retained morphometrics for the ten affected species now exactly match Cornell's source-supplied metric endpoints and cite only the corresponding Cornell measurement page.
- Hairy Woodpecker keeps immutable archive identity *Dryobates villosus* and explicitly discloses the source synonym *Leuconotopicus villosus*.
- Northern Flicker keeps immutable archive identity *Colaptes auratus* and explicitly scopes BirdLife's global status to its Yellow-shafted Flicker taxon.
- Yellow-throated and Red-eyed Vireo nest labels are `cup`; their sourced suspended-fork descriptions remain intact.
- Black-capped Chickadee is labelled `resident`; the sourced irruptive winter-movement qualification remains intact.

## Final model verdicts

- `openai/gpt-5.5-pro` via OpenRouter: **passed**, 40 species, review type `full-review-plus-hash-bound-delta`, zero findings.
- `x-ai/grok-4.5` via OpenRouter: **passed**, 40 species, review type `full-review-plus-hash-bound-delta`, zero findings.

Estimated total model-review spend, including route probes, full reviews and delta reviews: **$55.190493**. Usage receipts and raw model outputs are retained locally under ignored `.hermes/research/` and are not publication dependencies.
