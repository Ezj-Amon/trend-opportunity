# Evidence quality

- `official_notice`: 1.00
- `full_article`: 0.90
- `consumer_discussion`: 0.85
- `consumer_comment`: 0.75
- `manual_evidence`: 0.70
- `article_summary`: 0.55
- `search_snippet`: 0.30
- `title_only`: 0.10

A Bundle is ready only when it has at least two independent valid sources and at least one full article or official notice. `EVIDENCE_READY_SCORE` and the summed quality score are diagnostic only; they do not decide readiness. Source count alone cannot promote title-only evidence, search snippets do not count as independent valid sources, and syndicated near-duplicates count once.

Consumer voice is not mandatory for readiness, but its absence must remain explicit. Treat category similarity as exploratory retrieval—not probability, demand evidence, or a product conclusion.
