---
name: trend-opportunity-research
description: Research a Trend Opportunity Lab ResearchCandidate by collecting public evidence, rebuilding its EvidenceBundle, and producing a cited OpportunityAssessment that may abstain. Use when investigating a pending trend research direction, filling evidence gaps, comparing independent sources or consumer discussions, or resuming an interrupted ResearchRun. Do not use it to generate ProductHypotheses or recommendations.
---

# Trend Opportunity Research

Use the application's controlled Research and Evidence APIs. Never write the database directly.

The business boundary is defined by [docs/workflow-contract.md](../../docs/workflow-contract.md). This Skill is an execution rule set, not a second workflow specification. The repository currently has controlled ResearchRun tools and assessment providers, but no complete autonomous Research Agent or agent worker.

## Workflow

1. Read the ResearchCandidate, current EvidenceBundle, evidence items, ResearchRun budget, and prior tool calls.
2. Read [references/evidence-quality.md](references/evidence-quality.md) before deciding what evidence is missing.
3. Build fact-oriented queries. Do not search for product names, Amazon keywords, prices, or merchandise.
4. Collect official/public reporting first, then independent reporting, then public consumer discussions. Follow [references/source-routing.md](references/source-routing.md).
5. Invoke only controlled run tools. Record every call through the active ResearchRun; never store tokens, cookies, login pages, or credentials.
6. Rebuild the EvidenceBundle after adding evidence. Stop when the budget is exhausted or the Bundle remains insufficient.
7. Apply [references/abstention-rules.md](references/abstention-rules.md). With insufficient evidence, submit `insufficient_evidence`; do not infer missing facts.
8. With a ready Bundle, submit an OpportunityAssessment v2 matching [schemas/opportunity-assessment.json](schemas/opportunity-assessment.json). Judge, in order, whether this is a consumer change, whether it creates a concrete new problem, and whether it is worth further research. Record evidenced existing solutions and their gaps; leave them empty and add missing evidence when the Bundle does not support them. Cite database evidence IDs for every judgment, fact, and inference.
9. Wait for human review. Never approve the Assessment, invent product categories, create an OpportunitySignal, generate a ProductHypothesis, marketplace query, or recommendation.

## Controlled endpoints

- `GET /api/research-candidates/{candidate_id}`
- `POST /api/research-candidates/{candidate_id}/runs`
- `GET /api/research-runs/{run_id}`
- `POST /api/research-runs/{run_id}/tools/{tool_name}`
- `POST /api/research-runs/{run_id}/complete`
- `POST /api/research-candidates/{candidate_id}/assessments`
- `POST /api/research-candidates/{candidate_id}/assessments/cloud`

Resume an existing running ResearchRun instead of starting a duplicate executor run.
