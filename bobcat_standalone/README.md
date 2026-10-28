# BOBcat extraction pipeline: standalone version

The same code as the published `bobcat-parameter-extraction` skill (Oct 28 configuration), runnable from a terminal
with your own Anthropic API key, outside Claude Science.

## Contents
- `run_bobcat.py`: command-line entry point (runs papers in parallel, like `run_many` in the app)
- `host_shim.py`: stands in for the app's `host` object. Model requests go to the Anthropic API directly.
- `scripts/`: the pipeline (`bobcat_extract.py`, `bobcat_convert.py`, `bobcat_published.py`), unchanged from the skill
- `BOBcat v1.0 Model Parameter Master List 2.xlsx`: the template (latest version, with 'Dual AGN')
- `requirements.txt`

## One-time setup (macOS)
```bash
cd bobcat_standalone
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...        # create a key at console.anthropic.com; add this line to ~/.zshrc to keep it
```

## Running
```bash
source .venv/bin/activate
# every saved paper in your folder
python run_bobcat.py --all --papers-dir ~/Documents/GitHub/ai_bobcat_sandbox/html-papers
# selected papers (bibcode / arXiv id / DOI / path to a saved HTML or PDF)
python run_bobcat.py 2015MNRAS.454.1290K "$HOME/Documents/GitHub/ai_bobcat_sandbox/html-papers/AN OFF-CENTERED ACTIVE GALACTIC NUCLEUS IN NGC 3115 - IOPscience.html" \
       --papers-dir ~/Documents/GitHub/ai_bobcat_sandbox/html-papers --out runs/ngc3115
```
Outputs go to `--out` (default `runs/<date-time>/`), one folder per paper: one workbook per model (template layout plus
provenance, review_log and extraction_meta sheets) and a `(raw).json`. `run_summary.json` lists each paper's workbooks,
errors, and the total token use.

Notes:
- Pass bibcodes for papers whose saved file carries journal metadata. Pass arXiv-HTML pages without `citation_*` metadata
  (e.g. the Tick-Tock arXiv page) by file path. `--all` passes file paths.
- Image-only scans with no text layer (e.g. `1996ApJ...464L.107G.pdf`) stop with a 'text too short' error; that is expected.
- The arXiv lookups (submission dates, LaTeX fallback) need internet access to arxiv.org.

## Configuration
Every pipeline constant can be overridden with `--set NAME=VALUE`, e.g.
- `--set FP_ALLOW_RELABEL=True`: relabelling false-positive checker instead of drop-only
- `--set HEADER_REASK=False`: no Haiku name/redshift re-ask (in the Oct 28 tests the value extraction always gave both)
- `--set HARD_RULE_DROP=False`: keep forbidden-line-as-BLR entries for human review instead of dropping them
- `--set REQUIRE_EVIDENCE=False`: also write models that have no evidence entry
- `--set VALUES_MODEL='"opus-5-5"'`, `EVIDENCE_MODEL`, `FP_MODEL`, `HEADER_MODEL`: model choice by substring of the model id
Environment variables: `BOBCAT_UTILITY_MODEL` (used when a request names no model; default: first Haiku id) and
`BOBCAT_FALLBACK_MODEL` (default: first Sonnet id).

## Cost and speed
In this pipeline configuration (measured in the app on Oct 28), one paper used about 70-120k tokens: input plus output,
mostly paper text that is read three times (values, evidence, checker). Your bill follows Anthropic's API pricing for
those models. System prompts are marked for prompt caching, so repeat calls within a few minutes cost less. Wall time
was about 40-75 s for a batch of 4-8 papers. Outside the app there is no 2M-token ceiling. Your API account's rate limits
apply instead: lower `--concurrency` if you see rate-limit errors (the shim retries with backoff).

## Differences from running inside Claude Science
- The app wraps system prompts in its own envelope; here they are sent as written. Results should be equivalent but
  are not identical. LLM output also varies between runs, so every workbook remains a draft for human review.
- `host_shim.py` was tested offline with a mocked API client (the full pipeline ran and wrote a workbook). It has not
  yet been run against the live API; the first real run is the check.
- When the skill changes in the app, re-export this folder, or copy the three files from the skill's `scripts/`.
