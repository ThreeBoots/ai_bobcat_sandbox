#!/usr/bin/env python3
"""Run the BOBcat extraction pipeline from the command line, outside Claude Science.

Examples:
  export ANTHROPIC_API_KEY=...            # your own key (console.anthropic.com)
  python run_bobcat.py --papers-dir ~/Documents/GitHub/ai_bobcat_sandbox/html-papers --all
  python run_bobcat.py 2015MNRAS.454.1290K "/path/to/A paper - IOPscience.html" --out runs/today
"""
import argparse, glob, json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from host_shim import AnthropicHost


def load_pipeline(host):
    """Execute the three pipeline scripts (same files as the published skill) into one namespace."""
    g = {"__name__": "bobcat", "host": host}
    for f in ("bobcat_extract.py", "bobcat_convert.py", "bobcat_published.py"):
        path = os.path.join(HERE, "scripts", f)
        exec(compile(open(path).read(), path, "exec"), g)
    return g


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("papers", nargs="*", help="ADS bibcodes, arXiv ids, DOIs or paths to saved HTML/PDF files")
    ap.add_argument("--all", action="store_true", help="run every .html/.pdf file in --papers-dir")
    ap.add_argument("--papers-dir", default=None, help="folder of saved published versions (HTML/PDF)")
    ap.add_argument("--template", default=os.path.join(HERE, "BOBcat v1.0 Model Parameter Master List 2.xlsx"))
    ap.add_argument("--out", default=os.path.join("runs", time.strftime("%Y%m%d-%H%M")))
    ap.add_argument("--concurrency", type=int, default=8, help="parallel API requests (lower it if you hit rate limits)")
    ap.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                    help="override a pipeline constant, e.g. --set FP_ALLOW_RELABEL=True")
    a = ap.parse_args()

    ids = list(a.papers)
    if a.all:
        if not a.papers_dir: ap.error("--all needs --papers-dir")
        ids += sorted(p for p in glob.glob(os.path.join(os.path.expanduser(a.papers_dir), "*")) if p.lower().endswith((".html", ".htm", ".pdf")))
    if not ids: ap.error("no papers given")

    host = AnthropicHost(max_concurrency=a.concurrency)
    g = load_pipeline(host)
    for s in a.set:
        k, v = s.split("=", 1)
        g[k] = eval(v, {})                      # e.g. True, False, 2, "opus-5-5"
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()
    results, stats = g["run_many"](ids, a.template, out_root=a.out, papers_dir=os.path.expanduser(a.papers_dir) if a.papers_dir else None,
                                   max_concurrency=a.concurrency)
    tok = {k: sum(r[k] for r in host.usage_log) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")}
    summary = dict(seconds=round(time.time() - t0, 1), n_papers=len(ids), tokens=tok, api_calls=len(host.usage_log),
                   results=[dict(paper_id=r.get("paper_id"), workbooks=[os.path.basename(p) for p in (r.get("paths") or [])],
                                 seconds=r.get("seconds"), error=r.get("error")) for r in results])
    json.dump(summary, open(os.path.join(a.out, "run_summary.json"), "w"), indent=1, default=str)
    for r in summary["results"]:
        print(("ERROR " + str(r["error"])[:150]) if r["error"] else f"{len(r['workbooks'])} workbook(s)", "|", os.path.basename(str(r["paper_id"])))
    print(f"done in {summary['seconds']} s; tokens {tok}; outputs in {a.out}")


if __name__ == "__main__":
    main()
