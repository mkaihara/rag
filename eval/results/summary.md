## Results

| | cloud | local |
|---|---|---|
| Questions with evidence | 13 | 13 |
| MRR@6 | 0.71 | 0.77 |
| Recall@1 | 0.42 | 0.58 |
| Recall@3 | 0.81 | 0.85 |
| Recall@6 | 1.00 | 0.85 |
| Citations pointing to a returned source | 28/28 (100%) | 68/69 (99%) |
| Citations supported (judge) | 18/28 (64%) | 50/68 (74%) |
| Citations partly supported | 8/28 (29%) | 13/68 (19%) |
| Citations unsupported | 2/28 (7%) | 5/68 (7%) |
| Answers correct (graded by hand) | 11/13 (85%) | 6/13 (46%) |
| Answers partly correct | 2/13 (15%) | 4/13 (31%) |
| Says when the papers don't answer | 2/2 (100%) | 2/2 (100%) |
| Median time to first word | 1.5 s | 108.0 s |
| Median time per answer | 2.8 s | 143.1 s |
| Answers cut at the length limit | 0 | 0 |

## Setup

- **cloud** (2026-11-20): LLM `openai/gpt-6-luna`, embeddings `qwen/qwen3-embedding-8b`, top 6 sources, 120 chunks; citations judged by `anthropic/claude-sonnet-5.5`
- **local** (2026-11-20): LLM `qwen3.5:4b`, embeddings `qwen3-embedding:0.6b`, top 6 sources, 211 chunks; citations judged by `anthropic/claude-sonnet-5.5`

## Rank of the first relevant source

| Question | cloud | local |
|---|---|---|
| q01 | 4 | not in top 6 |
| q02 | 1 | 1 |
| q03 | 5 | 1 |
| q04 | 1 | 1 |
| q06 | 2 | 1 |
| q07 | 1 | 1 |
| q08 | 1 | 1 |
| q09 | 3 | 1 |
| q11 | 1 | 2 |
| q12 | 1 | 1 |
| q13 | 2 | not in top 6 |
| q14 | 1 | 1 |
| q15 | 2 | 2 |

With 13 questions, a difference of one or two questions is within noise. Retrieval metrics compare the embedding model and chunking; the answer metrics compare the whole systems.
