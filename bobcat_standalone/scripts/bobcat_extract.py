import json
"""BOBcat automated parameter extraction.

Pipeline:  template XLSX -> schema/vocab
           ADS bibcode | arXiv id -> LaTeX source text
           LLM (reading only) -> quantities *as stated* + verbatim quotes
           code -> unit/frame conversion, quote verification, range/vocab checks
           -> XLSX in the BOBcat Sheet1 layout (one sheet per model) + review log
"""
import io, os, re, json, math, tarfile, gzip, difflib, urllib.request, urllib.parse, threading, queue, time
import concurrent.futures
import pandas as pd

DAY_PER_YR = 365.25      # days per year for all year->day conversions (BOBcat guidelines: '=[years]*365.25')
MJD_MAX = 70000

# ----------------------------------------------------------------- template
def load_template(xlsx_path):
    x = pd.read_excel(xlsx_path, sheet_name=None, header=None)
    s1, s2 = list(x.values())[:2]
    s1 = s1.dropna(how="all").dropna(axis=1, how="all")
    s1.columns = s1.iloc[0]; s1 = s1.iloc[1:].reset_index(drop=True)
    s2 = s2.dropna(how="all").dropna(axis=1, how="all")
    s2.columns = s2.iloc[0]; s2 = s2.iloc[1:].reset_index(drop=True)
    pairs = s2[["Evidence type", "Evidence type note"]].dropna()
    vocab = {}
    for t, n in pairs.itertuples(index=False):
        vocab.setdefault(t.strip(), []).append(n.strip())
    wavebands = [w.strip() for w in s2["Evidence type wavebands"].dropna()]
    return dict(fields=s1, sheet2=s2, vocab=vocab, wavebands=wavebands)

# ----------------------------------------------------------------- fetching
_ARXIV_LOCK = threading.Lock(); _ARXIV_LAST = [0.0]
ARXIV_MIN_INTERVAL = 3.0   # seconds between arXiv API requests (arXiv's guidance for automated clients)

def _get(url):
    import subprocess
    if "arxiv.org" in url:                      # space arXiv requests even when many papers run at once
        with _ARXIV_LOCK:
            wait = ARXIV_MIN_INTERVAL - (time.time() - _ARXIV_LAST[0])
            if wait > 0: time.sleep(wait)
            _ARXIV_LAST[0] = time.time()
    r = subprocess.run(["curl", "-sfL", "--max-time", "90", url], capture_output=True)
    if r.returncode: raise IOError(f"curl failed ({r.returncode}) for {url}")
    return r.stdout

def arxiv_meta(arxiv_id):
    """-> dict(title, published YYYY-MM-DD of v1, journal_ref) from the arXiv API."""
    xml = _get("https://export.arxiv.org/api/query?id_list=" + re.sub(r"v\d+$", "", arxiv_id)).decode()
    e = xml.split("<entry>")[1]
    g = lambda t: (re.search(rf"<{t}[^>]*>(.*?)</{t}>", e, re.S) or [None, ""])[1]
    ver = (re.search(r"<id>http://arxiv.org/abs/[^<]+?(v\d+)</id>", e) or [None, ""])[1]
    return dict(title=" ".join(g("title").split()), published=g("published")[:10], journal_ref=g("arxiv:journal_ref"), version=ver)

def paper_filename(title, maxlen=180):
    """Filesystem-safe version of the paper title (BOBcat files are named after the paper)."""
    t = re.sub(r"\$|\\[a-zA-Z]+|[{}]", "", title or "untitled")
    t = re.sub(r'[\\/:*?"<>|]', "_", t)
    return " ".join(t.split())[:maxlen].rstrip(" .") or "untitled"

def bibcode_to_arxiv(bibcode):
    """Resolve an ADS bibcode (e.g. 2015MNRAS.454.1290K) to an arXiv id via the
    arXiv API journal-ref field. Returns (arxiv_id, title, journal_ref)."""
    m = re.match(r"(\d{4})([A-Za-z&.]+?)\.*(\d+)\.*([A-Z]?\d+)", bibcode)
    year, jour, vol, page = m.group(1), m.group(2).strip("."), m.group(3), m.group(4).lstrip("L")
    if int(year) < 1991: raise LookupError(f"{bibcode} predates arXiv")
    q = f'jr:"{vol}" AND jr:"{page}"'
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(
        {"search_query": q, "max_results": 50})
    xml = _get(url).decode()
    for e in xml.split("<entry>")[1:]:
        jr = re.search(r"<arxiv:journal_ref[^>]*>(.*?)</arxiv:journal_ref>", e, re.S)
        jtxt = jr.group(1) if jr else ""
        num_ok = re.search(rf"(?<!\d){vol}(?!\d)(?:\s*\(\d{{4}}\))?\D{{1,14}}(?:\d{{1,3}}\D{{1,6}})?L?{page}(?!\d)", jtxt)
        names = {"MNRAS": ["mnras", "monthly notices"], "ApJ": ["apj", "astrophys", "astrophysical j"], "ApJL": ["apj", "astrophys"],
                 "ApJS": ["apjs", "astrophys"], "Natur": ["nature"], "NatAs": ["nature astron"], "Sci": ["science"],
                 "A&A": ["a&a", "astron. astrophys", "astronomy and astrophysics", "astronomy & astrophysics"],
                 "AJ": ["aj ", "aj,", "astron. j", "astronomical journal"], "PhRvL": ["phys. rev. lett", "prl", "physical review letters"],
                 "PhRvD": ["phys. rev. d", "prd", "physical review d"], "PASJ": ["pasj"], "PASP": ["pasp"]}.get(jour, [jour.lower()[:4]])
        src_ok = any(n in jtxt.lower() for n in names)
        if jr and num_ok and src_ok:
            aid = re.search(r"<id>http://arxiv.org/abs/([^<]+?)(v\d+)?</id>", e).group(1)
            title = re.search(r"<title>(.*?)</title>", e, re.S).group(1)
            return aid, " ".join(title.split()), jr.group(1)
    raise LookupError(f"No arXiv match for {bibcode}; pass the arXiv id directly.")

def fetch_arxiv_tex(arxiv_id):
    """Download the arXiv source and return flattened LaTeX with comments removed."""
    raw = _get(f"https://arxiv.org/e-print/{arxiv_id}")
    texts = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
            for m in tf.getmembers():
                if m.name.endswith(".tex"):
                    texts[m.name] = tf.extractfile(m).read().decode("utf-8", "replace")
    except tarfile.ReadError:
        texts["main.tex"] = gzip.decompress(raw).decode("utf-8", "replace")
    main = next((k for k, v in texts.items() if "\\begin{document}" in v), list(texts)[0])
    tex = texts[main]
    for _ in range(3):  # inline \input / \include
        tex = re.sub(r"\\(?:input|include)\{([^}]+)\}",
                     lambda m: next((v for k, v in texts.items()
                                     if k.endswith(m.group(1)) or k.endswith(m.group(1) + ".tex")), ""), tex)
    tex = re.sub(r"(?<!\\)%.*", "", tex)                       # strip comments
    tex = tex.split("\\begin{thebibliography}")[0]              # drop references
    tex = re.sub(r"\n{3,}", "\n\n", tex)
    return tex

# ----------------------------------------------------------------- model selection
PREFERRED_MODEL = "sonnet-5-5"
EVIDENCE_MODEL = "opus-5-5"   # user choice: Opus 5.5 for the evidence-gathering call   # user choice: Sonnet 5.5 for every call except summary/caveat shortening

def _main_model(model=None):
    """Model for extraction, evidence, review, header re-ask and dropdown repair: an explicit model= wins;
    otherwise the available model id containing PREFERRED_MODEL; otherwise the platform reasoning default."""
    if model: return model
    try:
        hits = [m for m in host.list_models() if PREFERRED_MODEL in m]
        if hits: return sorted(hits, key=len)[0]
    except Exception:
        pass
    return host.reasoning_model()

_NO_FORCED_TOOL = set()   # models that reject tool_choice {"type": "tool"} (e.g. Sonnet 5.5)

# ----------------------------------------------------------------- batched model calls (run_many)
# host.llm calls made from different Python threads are serialized by the kernel-host channel, so papers run in
# threads would wait for each other. While a batcher is active, every model call in the pipeline is queued and
# sent together as ONE list-form host.llm call, which the host runs concurrently (max_concurrency, cap 32).
_LLM_BATCHER = None

class LLMBatcher:
    def __init__(self, max_concurrency=16, window=0.5):
        self.q = queue.Queue(); self.mc = min(int(max_concurrency), 32); self.window = window
        self.stats = []; self.calls = []; self._stop = False
        self._t = threading.Thread(target=self._loop, daemon=True); self._t.start()
    def submit(self, reqs=None, max_concurrency=None, **kw):
        single = not isinstance(reqs, list)
        if reqs is None: items = [dict(kw)]
        elif isinstance(reqs, str): items = [dict(prompt=reqs, **kw)]
        elif isinstance(reqs, dict): items = [{**reqs, **kw}]
        else: items = [dict(r) if isinstance(r, dict) else {"prompt": r} for r in reqs]
        futs = [concurrent.futures.Future() for _ in items]
        tag = threading.current_thread().name
        for it, f in zip(items, futs): it["_tag"] = tag; self.q.put((it, f))
        res = [f.result() for f in futs]
        if single:
            if isinstance(res[0], dict) and res[0].get("error"): raise RuntimeError(res[0]["error"])
            return res[0]
        return res
    def _loop(self):
        while not self._stop:
            try: batch = [self.q.get(timeout=0.2)]
            except queue.Empty: continue
            t_end = time.time() + self.window
            while True:
                rem = t_end - time.time()
                if rem <= 0: break
                try: batch.append(self.q.get(timeout=rem))
                except queue.Empty: break
            while True:
                try: batch.append(self.q.get_nowait())
                except queue.Empty: break
            t0 = time.time()
            tags = [b[0].pop("_tag", None) for b in batch]
            try: out = host.llm([b[0] for b in batch], max_concurrency=self.mc)
            except Exception as ex: out = [{"error": f"batch call failed: {ex}"}] * len(batch)
            dt = time.time() - t0
            us = [o.get("usage") or {} for o in out if isinstance(o, dict)]
            errs = [str(o.get("error"))[:200] for o in out if isinstance(o, dict) and o.get("error")]
            self.stats.append(dict(start=t0, n=len(batch), seconds=round(dt, 1), n_errors=len(errs), errors=errs[:5],
                                   models=sorted({str(b[0].get("model")) for b in batch}),
                                   input_tokens=sum(u.get("input_tokens", 0) or 0 for u in us),
                                   output_tokens=sum(u.get("output_tokens", 0) or 0 for u in us),
                                   max_output_tokens=max([u.get("output_tokens", 0) or 0 for u in us] or [0])))
            for (it, _), o, tg in zip(batch, out, tags):
                u = (o.get("usage") or {}) if isinstance(o, dict) else {}
                self.calls.append(dict(paper=tg, task=((it.get("tools") or [{}])[0].get("name") or "text"), model=str(it.get("model")),
                                       batch_start=t0, batch_seconds=round(dt, 1), batch_n=len(batch),
                                       input=u.get("input_tokens", 0) or 0, cache_write=u.get("cache_creation_input_tokens", 0) or 0,
                                       cache_read=u.get("cache_read_input_tokens", 0) or 0, output=u.get("output_tokens", 0) or 0,
                                       error=bool(isinstance(o, dict) and o.get("error"))))
            for (_, f), o in zip(batch, out): f.set_result(o)
    def close(self):
        self._stop = True; self._t.join(timeout=5)

def _host_llm(reqs=None, **kw):
    """All pipeline model calls go through here: direct host.llm normally, the active LLMBatcher inside run_many."""
    b = _LLM_BATCHER
    if b is not None: return b.submit(reqs, **kw)
    return host.llm(reqs, **kw) if reqs is not None else host.llm(**kw)

def _llm_tool(reqs, max_concurrency=2):
    """host.llm for structured (tool-use) requests that works whether or not the model accepts a forced tool_choice.
    Takes one request dict or a list; returns the same shape. Falls back to tool_choice 'auto' plus an explicit
    instruction, and retries once if no tool call comes back."""
    single = isinstance(reqs, dict); reqs = [dict(r) for r in ([reqs] if single else reqs)]
    def relax(r):
        r = dict(r); name = (r.get("tool_choice") or {}).get("name") or r["tools"][0]["name"]
        r["tool_choice"] = {"type": "auto"}
        r["system"] = (r.get("system") or "") + f"\n\nRespond ONLY by calling the tool `{name}` exactly once; no prose."
        return r
    reqs = [relax(r) if r.get("model") in _NO_FORCED_TOOL else r for r in reqs]
    out = _host_llm(reqs, max_concurrency=max_concurrency)
    redo = [i for i, o in enumerate(out) if isinstance(o, dict) and "tool_choice" in str(o.get("error", ""))]
    for i in redo: _NO_FORCED_TOOL.add(reqs[i].get("model")); reqs[i] = relax(reqs[i])
    if redo:
        for i, o in zip(redo, _host_llm([reqs[i] for i in redo], max_concurrency=max_concurrency)): out[i] = o
    missing = [i for i, o in enumerate(out) if isinstance(o, dict) and not o.get("error") and not o.get("tool_use")]
    if missing:
        for i, o in zip(missing, _host_llm([reqs[i] for i in missing], max_concurrency=max_concurrency)): out[i] = o
    return out[0] if single else out

# ----------------------------------------------------------------- LLM schema
ERROR_TYPES = {
    "Gaussian": "standard symmetric ± error given",
    "Two sided": "asymmetric +X/-Y errors given with X != Y",
    "Upper Limit": "value is an upper bound",
    "Lower Limit": "value is a lower bound",
    "Range": "a range of values is given (fill range_low/range_high)",
    "Assumed": "no error given AND the value is assumed to produce the model (e.g. circular orbit e=0), or adopted from another paper",
    "Representative": "catch-all when no error is given for the paper's own value: '~'/'\\approx' values, values from scaling relations, model results with lengthy derivations, values read off a plot",
}

_Q = {  # generic quantity as stated in the paper
    "type": "object",
    "properties": {
        "value": {"type": ["number", "null"], "description": "Numeric value exactly as stated, in `unit`. For limits, the bound. null if not given."},
        "err_plus": {"type": ["number", "null"]},
        "err_minus": {"type": ["number", "null"], "description": "Positive number."},
        "range_low": {"type": ["number", "null"]}, "range_high": {"type": ["number", "null"]},
        "unit": {"type": "string", "description": "Unit as stated, e.g. Msun, log_Msun, yr, day, pc, mpc, deg, Hz, MJD, dimensionless."},
        "frame": {"type": "string", "enum": ["observed", "rest", "n/a"], "description": "For periods/frequencies: observed (Earth) frame or source rest frame."},
        "error_type": {"type": "string", "enum": list(ERROR_TYPES)},
        "cited_from": {"type": "string", "description": "If adopted from another paper, that reference; else ''."},
        "quote": {"type": "string", "description": "VERBATIM text from the paper (LaTeX as-is) supporting the value. <=300 chars."},
        "note": {"type": "string", "description": "Brief note on derivation, ambiguity or why left null."},
    },
    "required": ["value", "unit", "error_type", "quote", "note"],
}
_EVID = {
    "type": "object",
    "properties": {
        "evidence_type": {"type": "string", "description": "The broad category of evidence (one of the allowed evidence types)."},
        "evidence_note": {"type": "string", "description": "The specific category for this evidence (one of the allowed evidence notes for the given evidence type)."},
        "waveband": {"type": "string", "description": "The waveband in which this evidence was observed (one of the allowed wavebands)."},
        "from_this_paper": {"type": "boolean", "description": "True if this paper presents the evidence; false if cited."},
        "quote": {"type": "string"},
    },
    "required": ["evidence_type", "evidence_note", "waveband", "from_this_paper", "quote"],
}
_PER = {"type": "object", "properties": {**_Q["properties"],
        "waveband": {"type": "string"},
        "epoch": {"type": "object", "description": "Observed period epoch: when THIS periodicity was measured (see rules).",
                  "properties": {"value": {"type": ["number", "null"]}, "range_low": {"type": ["number", "null"]},
                                 "range_high": {"type": ["number", "null"]}, "unit": {"type": "string", "description": "MJD, JD or decimal_year"},
                                 "error_type": {"type": "string", "enum": list(ERROR_TYPES)}, "quote": {"type": "string"}, "note": {"type": "string"}}}},
        "required": _Q["required"] + ["waveband"]}

QFIELDS = ["eccentricity", "total_mass", "m1", "m2", "chirp_mass", "reduced_mass", "mass_ratio_q",
           "orbital_inclination", "semi_major_axis", "separation", "angular_separation", "orbital_period",
           "orbital_frequency", "period_epoch"]

def build_tool(tpl):
    model = {"type": "object", "properties": {
        "model_label": {"type": "string", "description": "Short label distinguishing this parameter set."},
        **{k: _Q for k in QFIELDS},
        "source_nickname": {"type": "string", "description": "Only if this model is for a different source than the top-level source_nickname (multi-candidate papers)."},
        "redshift": {"type": ["number", "null"], "description": "Only if this model's source differs from the top-level source."},
        "observed_periods": {"type": "array", "items": _PER, "description": "One entry per distinct observed periodicity (no limit)."},
        "summary": {"type": "string", "description": "Summary of the paper as it concerns this source, under 750 characters."},
        "caveats": {"type": "string", "description": "Scientific reasons that WEAKEN the SMBHB interpretation (see rules)."},
        "external_caveats": {"type": "string", "description": "Caveats from outside this paper (with references); kept off the sheet."},
        "extraction_notes": {"type": "string", "description": "Bookkeeping notes on how values were mapped to the catalogue (cited values, limits, fields left empty, quantities with no catalogue field). NOT caveats."},
    }, "required": ["model_label", *QFIELDS, "observed_periods", "summary", "caveats", "extraction_notes"]}
    return {"name": "record_bobcat_models",
            "description": "Record SMBHB model parameters extracted from the paper.",
            "input_schema": {"type": "object", "properties": {
                "paper_title": {"type": "string", "description": "Title of the paper as printed (required)."},
                "source_nickname": {"type": "string", "description": "REQUIRED, never empty: name of the paper's main SMBHB candidate source (see the source_nickname/redshift rules)."},
                "redshift": {"type": ["number", "null"], "description": "REQUIRED: redshift of the main source as stated anywhere in the paper (see the redshift rule); null only if the paper states none."},
                "redshift_quote": {"type": "string", "description": "Verbatim text (sentence or table cell) containing the recorded redshift; if redshift is null, state that the paper gives none."}, # NOTE: [IGNORE] May want to get this via NED call, i.e. convert to Earth frame later in the pipeline
                "journal_received_date": {"type": ["string", "null"], "description": "Date the journal received/submitted the paper as stated in the text (YYYY-MM-DD), else null."},
                "paper_role": {"type": "string", "enum": ["binary_model", "disproves_binary_evidence", "non_binary_alternative_only", "pta_limit_only"],
                               "description": "binary_model: proposes/adopts/constrains a binary model; disproves_binary_evidence: shows data/analysis behind a binary model was flawed (still a BOBcat entry); non_binary_alternative_only: only proposes a non-binary origin, OR proposes a binary that does not meet the BOBcat definition, e.g. either BH < 10^6 Msun (both must be >= 10^6 Msun) or separation > 1 kpc (NOT a BOBcat entry); pta_limit_only: only targeted PTA limits."},
                "paper_role_reason": {"type": "string"},
                "pta_targeted_limit": {"type": "boolean", "description": "True if the paper performs a targeted PTA limit on the source(s) (listed separately as a 'PTA limit' paper)."},
                "models": {"type": "array", "items": model, "minItems": 1}},
                "required": ["paper_title", "source_nickname", "redshift", "paper_role", "paper_role_reason", "pta_targeted_limit", "models"]}}

FIELD_RULES = """- 'orbital_inclination' is the inclination of the BINARY ORBIT. A jet viewing angle, galaxy inclination, or other angle provided is NOT an orbital
  inclination unless the paper explicitly states they are equal (not merely that the jet, etc. is related to the orbit).
- 'separation': the binary separation quoted in the paper, either an observed projected separation or an
  orbital separation. It must be the separation between the two SMBHs (e.g. between two AGN). An offset of a black
  hole or AGN from the centre of its host galaxy (photometric, kinematic or dynamical centre, bulge or stellar nucleus)
  is NOT a separation, unless the paper itself uses that offset as the separation of (or a proxy for the separation
  of) the two black holes; in that case record it and say so in the note. Use 'semi_major_axis' when the paper
  explicitly calls the quantity a semi-major axis. In case of a circular orbit, the semi-major axis may be called
  "radius," "orbital radius," or another indirect description; record such a value as semi_major_axis.
  Definitions (BOBcat): 'semi_major_axis' is half the longest diameter of the (elliptical) orbit; for a circular orbit
  it is synonymous with the radius of the orbit. 'separation' is always the current separation of the two black holes
  (observed projected, or orbital); for a circular orbit it is the diameter of the orbit, i.e. twice the semi-major axis.
  Record each quantity in its own field as stated; do not derive one from the other.
  Authors use symbols such as a, r, d or R inconsistently (a may denote the separation in one paper and the semi-major
  axis in another): decide which field a length belongs to from the paper's words and definitions, never from its
  symbol alone.
- 'angular_separation': the observed angular (on-sky) separation between the two black holes of THIS binary (e.g. two
  nuclei, radio cores or emission-line centroids), as stated, with its unit (arcsec, mas or microarcsec). It must be
  the separation between the two SMBHs, each located by its own observed position. An offset of a black hole or AGN
  from the centre of its host galaxy (photometric, kinematic or dynamical centre, bulge or stellar nucleus) is NOT an
  angular separation, whatever the paper assumes lies at that centre; neither are separations involving other
  components, or beam/resolution sizes. In such cases leave angular_separation empty (the offset may be mentioned in
  extraction_notes). Record it as stated; do not convert between angular and physical separation (a physical
  separation stated in the paper goes in 'separation').
- 'redshift' (fills the 'paper redshift' row): the redshift given in the paper for the system under study, as stated,
  with a verbatim quote in redshift_quote. It is also used to convert rest-frame periods to the Earth frame.
  Search the WHOLE paper for it (abstract, introduction, data/observation sections, tables and table notes,
  footnotes); a value the paper cites from another work or a database counts. If several values are given, use the
  one the paper adopts for its analysis; if it states no preference, the most precise one. redshift_quote must be
  verbatim and contain the number recorded in redshift. Never derive a redshift from a distance or a velocity; if the
  paper states no redshift for the source, set redshift = null and say so in redshift_quote.
- 'period_epoch': epoch of the proposed ORBITAL period (fill only when orbital_period is filled; observed
  periodicities carry their own epoch, see below). If stated as a single epoch, report it with its unit
  (MJD, JD or decimal_year). Otherwise, if the observation dates of the data are given, set range_low/range_high
  = start/end of the full time span of ALL OBSERVATIONS analysed IN THIS PAPER (never predicted/future dates;
  all instruments/epochs combined, INCLUDING archival data the paper uses or re-analyses;
  unit decimal_year or MJD) with error_type='Range'; code stores the midpoint 
  with the half-width as error. This applies even if the period itself was taken from another paper. If no dates 
  can be found at all, leave it null: code falls back to the journal submission (received) date.
- 'observed_period' values are the periodicities measured in TIME-SERIES data 
  (light curves, velocity curves, VLBI component kinematics), with waveband. These are OBSERVED, not CALCULATED quantities,
  though statistical methods may be applied to find them. Timescales inferred from spatial structure or theory 
  are NOT observed periods. State the frame precisely (observed vs rest).
  include every periodicity the model relies on, including ones measured in another paper (set cited_from).
  waveband: the OBSERVER-frame band of the data in which the periodicity was measured (the instrument/data band), and
  exactly ONE waveband per observed-period entry: a periodicity measured in several wavebands gets one entry per band
  (same period, epoch and quote basis), repeated as needed to cover the stated wavelength range (the downstream database
  allows one waveband per entry).
  Each observed period has its own 'epoch' (the observed period epoch): a stated epoch of that periodicity if
  given, otherwise range_low/range_high = start/end of the FULL light curve / time series in which THAT periodicity
  was measured (its first and last data points, including archival data in that time series; not only the span of
  the individual outbursts, cycles or features used to time the period) (unit decimal_year or MJD, error_type
  'Range'); null if no dates (code falls back to the submission date).
- 'orbital_period' is the binary orbital period, which may or may not exactly match any observed periods. This is a CALCULATED quantity.
  Orbital periods from a physical binary model (e.g. from Kepler's law or simulations) are in the SOURCE REST
  FRAME unless the paper explicitly says observer frame; follow the paper's own frame labels where given.
- If the paper states the SAME period in both the observed (Earth) frame and the rest frame, report the
  observed-frame statement (frame='observed'); use a rest-frame value only when no observed-frame value is given.
- If the frame of an OBSERVED period (a periodicity measured from time-series data) is unclear, check if it is
  associated with the caption of a time series plot. Time series data will usually be in the observer frame, so if the
  period is directly pulled from a provided time series plot, it is likely in the observer frame as well. This does NOT
  apply to model orbital periods (from Kepler's law, simulations or model fits): they keep the rest-frame default
  unless the paper explicitly labels their frame, even when a light-curve figure or caption is nearby.
- cited_from: set it whenever the paper attributes a value to another work, including table footnotes,
  or values derived by the paper purely from another work's inputs.
- Every non-null value needs a verbatim quote. eccentricity=0 (Assumed) only when the paper states a circular
  orbit or uses an explicitly circular-orbit relation; quote that statement or equation.
- mass_ratio_q is m2/m1 <= 1. If a mass ratio is stated in the paper as a value > 1, save the value as the reciprocal.
- total_mass: a modelled M1+M2, or a host-based mass (dynamical, scaling relation); a paper's "black hole mass"
  also counts as total mass. Always state in the note (and extraction_notes) HOW the mass was determined.
- eccentricity: if the model assumes a circular orbit, eccentricity = 0 with error_type 'Assumed'.
- Do NOT extract quoted GW strain values (computed downstream from model parameters).
- A source can have several models: emit one model per distinct parameter set the paper proposes (e.g. alternative
  scenarios), for every source it covers. When the paper covers several sources, set source_nickname/redshift on
  each model.
- observed_periods: only periodicities the binary interpretation relies on (e.g. the outburst or orbital-modulation
  period). Never short variability or correlation timescales (minutes-days flickering, flare decay times).
- observed_periods: the observations must cover at least one full period for it to count as an observed period.
  A period fitted to data covering only part of a cycle, or a predicted periodic signature that has yet to be
  observed, is NOT an observed period.
- source_nickname: the source name only, as used in the paper (no descriptions or component labels in brackets);
  use the designation the paper adopts for the object (e.g. a short name it introduces for it).
- Top-level source_nickname and redshift are REQUIRED in every answer, also for single-model papers: they describe
  the paper's main SMBHB candidate (in a multi-source paper, the source with the most detailed binary model). Never
  leave them out or empty. When the paper covers several sources, ALSO set source_nickname and redshift on every
  model, each for that model's own source.
- When the paper gives a best-fit value AND a range, fill value plus range_low/range_high (code sets the error type).
- Do NOT compute quantities that follow directly from other extracted ones (orbital frequency from period,
  q / chirp / reduced / total mass from m1 and m2, etc.); the ingestion pipeline derives these. Fill such a field
  only if the paper itself states that value.
- Parameters proposed in OTHER papers may be included (set cited_from) if they are consistent with the current
  paper's model, e.g. adopted by it. EXCLUDE any value from another paper that the current paper's model
  explicitly contradicts or supersedes. All parameters in one model must be mutually consistent.
- When similar values are given for the same parameter (such as a mass or period being mentioned in varying degrees of
  shorthand throughout a paper, or two different estimation methods yielding a similar value), prioritize which value to
  record by whether it has error values first, then by which has the smallest error range, and then by the precision of
  the value in significant figures. If two values are still equally ranked after these criteria (and the error-type
  priority below), record the one that appears LATER in the paper.
- Values with numerical error types assigned (i.e. Gaussian, Two sided, Range) should always be prioritized over
  Upper/Lower Limits, which in turn should be prioritized over Representative/Assumed values when more than one are
  given for the same quantity.
- error_type (choose exactly one):
{etypes}"""

SYSTEM = """You are an expert extragalactic astronomer extracting supermassive black hole binary (SMBHB)
model parameters from a paper for the BOBcat catalogue. SMBHB = gravitationally bound pair, each BH >=1e6 Msun,
separation <1 kpc. A "model" is one self-consistent parameter set the authors propose or adopt for the source;
emit several models only if the paper presents genuinely distinct parameter sets (e.g. different scenarios).
If the paper presents more than {max_models} models, record the first {max_models} in full; the remaining ones are
extracted in later passes (do not abbreviate or merge models to fit them in).

Rules:
- Report every number EXACTLY as stated, with its stated unit and frame. Do NOT convert units, take logs,
  or change frames; downstream code does that.
- Only fill a field if the paper states or adopts that quantity for the binary. Otherwise value=null with a note.
- Classify the paper (paper_role). Papers that only propose a non-binary origin are not BOBcat entries; papers
  showing that the data behind a binary model were flawed ARE entries (their models/caveats record that).
- If the paper does propose a binary system, but the model proposed does not fit the BOBcat definition of a binary,
  it should be considered as a non-binary model paper (paper_role='non_binary_alternative_only'). For example, if
  the model put forward by a paper suggests that either black hole has a mass < 10^6 M_sun (BOTH must be >= 10^6 M_sun;
  exactly 10^6 M_sun counts), or a separation > 1 kpc, it is effectively
  arguing against the source being a BOBcat binary.
{field_rules}
- quote must be copied verbatim from the provided text (LaTeX included), short, and contain the number.
- Evidence types are recorded by a separate step; do not report evidence here.
- summary and caveats: each strictly under 750 characters.
- caveats: brief. ONLY information STATED IN THIS PAPER that weakens the claim that the source
  is a true SMBHB (degenerate evidence, model inconsistencies, alternative explanations the authors raise,
  predicted signals not seen, follow-up needed).
- external_caveats: optional, brief: caveats from outside knowledge (e.g. later data not confirming a
  prediction), each naming a specific real reference. Kept out of the sheet for human review; never invent.
- extraction_notes: catalogue bookkeeping (cited values, limits, empty fields, parameters with no catalogue
  field). These are NOT caveats.
"""

def _field_rules():
    return FIELD_RULES.format(etypes="\n".join(f"    {k}: {v}" for k, v in ERROR_TYPES.items()))

HEADER_TOOL = {"name": "record_header", "description": "Source name and redshift.",
    "input_schema": {"type": "object", "properties": {
        "source_nickname": {"type": "string", "description": "Name of the source as used in the paper."},
        "redshift": {"type": ["number", "null"]}, "redshift_quote": {"type": "string", "description": "Verbatim."}},
        "required": ["source_nickname", "redshift", "redshift_quote"]}}

HEADER_TEXT_CHARS = 20000   # the header re-ask reads only the opening of the paper (title, abstract, introduction)

def _values_model(model=None):
    """Explicit model= wins; else VALUES_MODEL; else _main_model()."""
    return model or (_resolve_model(VALUES_MODEL) if VALUES_MODEL else _main_model())

def _fill_header(ext, tex, model):
    ms = ext.get("models") or []
    if not ext.get("source_nickname"):
        ext["source_nickname"] = next((m.get("source_nickname") for m in ms if isinstance(m, dict) and m.get("source_nickname")), None)
    if ext.get("redshift") is None:
        z = next((m.get("redshift") for m in ms if isinstance(m, dict) and isinstance(m.get("redshift"), (int, float))), None)
        if z is not None: ext["redshift"] = z
    if isinstance(ext.get("redshift"), str):
        try: ext["redshift"] = float(ext["redshift"])
        except ValueError: pass
    missing = [k for k in ("source_nickname", "redshift") if ext.get(k) in (None, "")]
    if "redshift" in missing and "redshift" in ext and str(ext.get("redshift_quote") or "").strip():
        missing.remove("redshift")                  # explicit null with an explanation: the paper states no redshift
    if not missing: ext["_header_source"] = "value_extraction"; return ext
    ext["_header_missing"] = missing
    if not HEADER_REASK:
        ext["_header_source"] = "value_extraction (re-ask disabled; missing: " + ", ".join(missing) + ")"; return ext
    r = _llm_tool(dict(prompt="PAPER TEXT (opening of the paper):\n\n" + tex[:HEADER_TEXT_CHARS], tools=[HEADER_TOOL],
                 system="Report the name of the (main) SMBHB candidate source as used in this paper and its redshift as stated (null if absent), with a verbatim quote.",
                 tool_choice={"type": "tool", "name": "record_header"}, model=_resolve_model(HEADER_MODEL) if HEADER_MODEL else _main_model(model), max_tokens=4000))   # 500 was too few: default thinking can use the whole budget
    tu = r.get("tool_use") if isinstance(r, dict) else None; tu = tu[0] if isinstance(tu, list) and tu else tu
    if not isinstance(tu, dict):                    # no usable answer: keep what the main extraction found
        ext["_header_refill_failed"] = str((r or {}).get("error") or "no tool call")[:200] if isinstance(r, dict) else "no result"
        return ext
    h = _unwrap(tu["input"] if "input" in tu else tu, ["source_nickname", "redshift"])
    for k in ("source_nickname", "redshift", "redshift_quote"):
        if ext.get(k) in (None, ""): ext[k] = h.get(k)
    ext["_header_refilled"] = True
    return ext

# ----------------------------------------------------------------- evidence step (separate model call)
# One-line definitions for the Sheet2 notes (draft, user-editable). Notes added to Sheet2 later simply have no definition.
EVIDENCE_NOTE_GUIDE = {
    ('spectral_line_variability', 'Broad-line velocity shifts'): 'broad emission line centroid/peak velocity changes during or between observations/over time (e.g. a drifting broad emission line), interpreted as orbital motion. Almost always originates in the broad-line region.',
    ('spectral_line_variability', 'Narrow-line velocity shifts'): 'narrow emission line velocity changes during or between observations/over time. Usually originates in the narrow-line region.',
    ('spectral_line_variability', 'Other BLR/NLR (e.g. sudden appearance of additional line feature)'): 'other multi-epoch line-profile changes (new components appearing, profile changes over time)',
    ('spectral_line_snapshot', 'Multiple narrow-line peaks'): 'double/multiple-peaked narrow emission lines in a single spectrum as a result of distinct, usually unresolved narrow-line regions associated with each black hole in the binary.',
    ('spectral_line_snapshot', 'Multiple broad-line peaks'): 'double/multiple-peaked broad lines in a single spectrum as a result of distinct, usually unresolved broad-line regions associated with each black hole in the binary.',
    ('spectral_line_snapshot', 'Other abnormal BLR (e.g. asymmetries)'): 'single-epoch broad-line oddities, such as broad lines offset in velocity from the systemic redshift, asymmetric or shifted broad profiles',
    ('spectral_line_snapshot', 'Other abnormal NLR'): 'single-epoch narrow-line oddities other than multiple peaks',
    ('continuum_variability', 'Continuous light curve variation with periodicity'): 'a continuous light-curve modulation that is periodic or quasi-periodic (sinusoid-like oscillation, repeating cycles). Differentiated from discrete bursts by the slow growth and decay of the light curve signal.',
    ('continuum_variability', 'Discrete bursts with periodicity'): "TWO OR MORE discrete bursts/flares/outbursts (flux increases) recurring periodically or quasi-periodically, but not sinusoidally. Typically related to flaring events or outbursts, with steep climbs and dropoffs in the light curve.",
    ('continuum_variability', 'Correlated multi-band variations'): 'variability correlated between wavebands that the paper uses as binary evidence. Record one entry for EVERY waveband included in the correlation statement.',
    ('continuum_variability', 'Other continuum flux variation'): 'other continuum behaviour used as evidence that is neither a periodic modulation nor periodically recurring bursts (e.g. dips, a single flare, non-periodic changes)',
    ('spatially_resolved_offset_or_dual_AGN', 'Dual AGN'): 'two spatially resolved AGN, i.e. two resolved AGN emission regions (each an accreting SMBH system: SMBH, disk, torus, BLR/NLR). Two resolved stellar cores or bulges, even in an active galaxy, are NOT dual AGN: they are host_galaxy "Morphological: e.g. tidal tails, asymmetry, dual stellar core"',
    ('spatially_resolved_offset_or_dual_AGN', 'Active nucleus offset from photometric or kinematic center'): 'AGN (the usually point-like accreting SMBH system) spatially offset from the photometric or kinematic centre of the host galaxy',
    ('spatially_resolved_offset_or_dual_AGN', 'Other resolved nucleus feature'): 'other spatially resolved structure of the AGN (nucleus in the AGN sense: the SMBH system and its emission regions) used as evidence',
    ('pc_jet_morphology', 'Helical structure'): 'helical/wiggling pc-scale (VLBI) jet',
    ('pc_jet_morphology', 'Time-resolved jet change'): 'jet properties, such as morphological structure, kinematic components, or luminosity changing over the course of observations.',
    ('pc_jet_morphology', 'CSO/CSS source'): 'spatially resolved (VLBI) compact symmetric object / compact steep-spectrum source',
    ('pc_jet_morphology', 'Other pc-scale jet feature'): 'other pc-scale jet property used as evidence',
    ('kpc_jet_morphology', 'X/S/Z/helical shaped sources'): 'X-, S-, Z-shaped or helical kpc-scale radio structure',
    ('kpc_jet_morphology', 'Spatial periodicity'): 'periodic spatial structure along a large-scale jet',
    ('kpc_jet_morphology', 'Other large-scale jet feature'): 'other kpc-scale jet property used as evidence',
    ('host_galaxy', 'Morphological: e.g. tidal tails, asymmetry, dual stellar core'): 'merger morphology of the host galaxy: tidal tails, asymmetry of the host, or two resolved stellar cores (galactic nuclei/bulges), each indicating a recent galaxy merger',
    ('host_galaxy', 'Flat-cored galaxy/light deficit'): 'core light deficit attributed to binary scouring',
    ('host_galaxy', 'Enhanced tidal disruption rates'): 'elevated tidal disruption rates attributed to a binary',
    ('host_galaxy', 'Very massive galaxy'): 'an exceptionally massive host that the paper uses as binary evidence',
    ('host_galaxy', 'Other secondary merger indicators (incl. heightened star formation, ULIRG, etc)'): 'other merger signatures used as evidence, such as heightened star formation, ULIRG, etc',
    ('gravitational_waves', 'PTA: GW memory'): 'pulsar-timing gravitational-wave memory signal',
    ('gravitational_waves', 'PTA: Continuous waves'): 'pulsar-timing continuous-wave signal',
    ('gravitational_waves', 'Space: GW memory'): 'space-interferometer GW memory signal',
    ('gravitational_waves', 'Space: Continuous waves'): 'space-interferometer continuous waves',
    ('gravitational_waves', 'Other GW emission'): 'other GW detection/claim',
    ('SED_feature', 'Other broad-band SED feature'): 'SED features (e.g. notches/deficits from a cavity) used as binary evidence',
}

EVIDENCE_RULES = """- Evidence must be about THIS binary candidate (the source the model describes): never features of other objects the
  paper cites for comparison, nor of other components of the system unless the
  paper uses them as evidence for this binary. Include every observation the paper presents as supporting, relied on
  for, or consistent with the binary interpretation, INCLUDING brief corroborating remarks. Brief remarks count only if
  they report an observation or measurement of THIS source; general statements about the class of object, or
  theoretical expectations, are not evidence. Exclude generic AGN properties and observations unrelated to the binary argument.
- The evidence types classify the observed phenomena in order to identify the PHYSICAL SCENARIO producing them. Choose
  the note whose implied physical scenario matches what the paper describes, even if the observed phenomenon differs
  somewhat from the note's typical case.
- Be careful to ensure that each piece of evidence recorded is evidence for a BINARY system, not just for the presence
  of a supermassive black hole, an AGN, etc.
- Be sure that the evidence recorded is for a binary hypothesis, and does not solely support a single black hole,
  ejected black hole, recoiling black hole, etc. model.
  Features compatible with BOTH a binary and an alternative scenario (e.g. a recoiling or ejected black hole) ARE
  included; exclude only evidence the paper uses
  solely for the alternative scenario.
- 'spectral_line_variability' requires line changes between epochs; a single-epoch offset or profile is
  'spectral_line_snapshot'.
- Evidence notes containing 'with periodicity' require an actually observed periodicity or quasi-periodicity
  (repeated cycles).
- 'Discrete bursts with periodicity' is ONLY for two or more BURSTS (flux increases: flares,
  outbursts) recurring periodically or quasi-periodically. Dips or a single flare are
  never 'bursts with periodicity': use 'Other continuum flux variation' (or the analogous 'Other' note).
  If there are two or more bursts, but no period is explicitly suggested, check for language indicative
  of periodic behavior. If the bursts are related to the binary orbit, said to be repeating, or said to be caused by some other periodic
  mechanism, then 'Discrete bursts with periodicity' is an appropriate categorization.
  The same is true of 'Continuous light curve variation with periodicity'. If a slow, continuous increase and decrease in flux/luminosity
  is observed to repeat, but no period is suggested, apply the above procedure to determine whether it should be classified as
  'Continuous light curve variation with periodicity' or 'Other continuum flux variation'.
  If a light curve shows a continuous periodic or quasi-periodic oscillation WITH strong burst (flare/outburst) features,
  record BOTH 'Continuous light curve variation with periodicity' and 'Discrete bursts with periodicity' (user rule).
- 'Correlated multi-band variations': record one entry for EVERY waveband included in the correlation statement.
- Be particularly vigilant for time resolved variation/variability in papers which have data taken over a long (>3 yr) timespan,
  as sometimes explicit statements of variability will be shown in figures and never explicitly stated in the text. Look for wording
  indicative of time-resolved changes, such as discussions of increasing/decreasing flux, morphological/kinematic evolution, etc.
  If a large amount of data taken over a long time is used, there is usually a reason for it.
- An emission line, broad or narrow, whose profile contains two or more velocity components counts as multiple peaks
  ('Multiple broad-line peaks' / 'Multiple narrow-line peaks'), whether or not the paper uses the word 'peak'.
- Terminology in the evidence types and notes: 'AGN' and 'active nucleus' always mean the region producing AGN emission,
  i.e. the (usually point-like) SMBH system: the SMBH, its accretion disk, torus, BLR/NLR, etc. 'Core', 'nucleus' and
  'bulge' (without 'active') mean the galactic nucleus of the host, i.e. its stellar component. This applies to every
  note that refers to AGN or host-galaxy properties, EXCEPT the notes of type 'spatially_resolved_offset_or_dual_AGN':
  there 'nucleus' (e.g. 'Other resolved nucleus feature') is used in the AGN sense.
- Forbidden lines (conventionally written in square brackets) never arise in a broad-line region. A feature seen in a
  forbidden line belongs to the narrow-line region (or to some other, non-BLR system): never use a broad-line note for it.
  This also applies to the absence, weakness or upper limits of forbidden or narrow lines: such statements describe the
  narrow-line region, so they take a narrow-line note, never a broad-line note.
- If the paper explicitly presents the source as a dual AGN or candidate dual AGN (two AGN, as defined above), record 'Dual
  AGN' for the observations showing the two components (user rule, pending a redefinition of the spatially
  resolved categories).
- Evidence notes: avoid 'Other ...' notes whenever the evidence can, even generously, be classified with a specific
  note; use 'Other ...' only when no specific note fits.
- waveband: the OBSERVER-frame band in which the feature was observed, i.e. the band of the instrument/data, never
  the rest-frame band of the emission.
- Evidence observed in several wavebands gets ONE ENTRY PER WAVEBAND: repeat the same type and note once for every band
  needed to cover the full wavelength range the paper states. Never put several wavebands in one entry (the downstream
  database allows exactly one waveband per entry).
- The same data can support more than one entry only when it shows genuinely different features. Otherwise one entry per feature.
  That is, do not list the same exact set of evidence type, note, and waveband more than once. However, it is OK to list the same
  evidence type and note twice if the waveband is different, and it is OK to use the same data (i.e. same evidence type and waveband)
  more than once if it contains more than one specific detail that supports the binary claim (i.e. a different evidence note).
- One passage may support SEVERAL entries when it indicates several pieces of evidence: record each (user rule). Jet
  structure reported on BOTH pc scales and kpc scales gets both the pc-scale note (pc_jet_morphology) and the kpc-scale note (kpc_jet_morphology).
- Evidence is per SOURCE: it applies to every model of that source. For multi-candidate papers, give each source
  its own list.
- Include evidence measured in other papers that this paper relies on for the binary case (from_this_paper=false).
- Predictions of future behaviour are not evidence.
- Check every entry's label before recording it, as a reviewer would: (1) it concerns THIS source; (2) single-epoch
  versus multi-epoch data decides spectral_line_snapshot versus spectral_line_variability; (3) the waveband is the
  observer band of the instrument/data that observed it; (4) of all allowed notes, the chosen type and note fit the
  definitions best, and every condition in the note's definition ('only', 'never', 'requires', number of events, etc.)
  is met. If a specific note's conditions are not met, use the note that fits (an 'Other ...' note only when no specific
  note does)."""

EVIDENCE_SYSTEM = """You are an expert extragalactic astronomer classifying the observational EVIDENCE a paper uses to argue that a
source hosts a supermassive black hole binary, for the BOBcat catalogue. Report only evidence; numbers are handled
elsewhere. Use ONLY these types and notes (type -> note: definition):
{guide}
Allowed wavebands (one per entry; split multi-band evidence into one entry per band): {wavebands}.
Rules:
{rules}
- quote: verbatim text from the paper (LaTeX as-is, <=300 chars) showing the feature."""

EVIDENCE_TOOL = {"name": "record_evidence", "description": "Evidence entries per source.",
    "input_schema": {"type": "object", "properties": {"sources": {"type": "array", "items": {"type": "object", "properties": {
        "source_nickname": {"type": "string"}, "evidence": {"type": "array", "items": _EVID}},
        "required": ["source_nickname", "evidence"]}}}, "required": ["sources"]}}

def _evidence_guide(tpl):
    lines = []
    for t, notes in tpl["vocab"].items():
        lines.append(f"  {t}:")
        for n in notes:
            dfn = EVIDENCE_NOTE_GUIDE.get((t, n), "")
            lines.append(f"    - {n}" + (f": {dfn}" if dfn else ""))
    return "\n".join(lines)

def _evidence_model(model=None):
    """Explicit model= wins; else the available id containing EVIDENCE_MODEL; else _main_model()."""
    if model: return model
    try:
        hits = [m for m in host.list_models() if EVIDENCE_MODEL and EVIDENCE_MODEL in m]
        if hits: return sorted(hits, key=len)[0]
    except Exception:
        pass
    return _main_model()

def _evidence_request(tex, tpl, evidence_model=None, max_tokens=8000):
    sys = EVIDENCE_SYSTEM.format(guide=_evidence_guide(tpl), wavebands=", ".join(tpl["wavebands"]), rules=EVIDENCE_RULES)
    if not FP_ALLOW_RELABEL:      # drop-only checker: nothing corrects labels after this call
        sys += ("\n- The labels you choose are final: a later check only removes entries that are not evidence; it does not "
                "correct types, notes or wavebands.")
    return dict(prompt="PAPER TEXT:\n\n" + tex, system=sys, tools=[EVIDENCE_TOOL],
                tool_choice={"type": "tool", "name": "record_evidence"},
                model=_evidence_model(evidence_model), max_tokens=max_tokens)

def _parse_evidence(r):
    if not isinstance(r, dict) or r.get("error"):
        return [], f"evidence step failed: {r.get('error') if isinstance(r, dict) else r}"
    tu = r.get("tool_use"); tu = tu[0] if isinstance(tu, list) else tu
    tu = tu or {}
    srcs = _coerce_list(_unwrap(tu.get("input", tu), ["sources"]).get("sources", []), "sources")
    for s in srcs:
        out = []
        for e in _coerce_list(s.get("evidence", []), "evidence"):
            w = e.get("waveband") if isinstance(e, dict) else None
            parts = [x.strip() for x in re.split(r"\s*(?:,|;|/|&|\band\b|\+)\s*", w) if x.strip()] if isinstance(w, str) else []
            if len(parts) >= 2: out += [dict(e, waveband=x, _split_from=w) for x in parts]   # one entry per waveband
            else: out.append(e)
        s["evidence"] = out
    return srcs, ""

EVIDENCE_RUNS = 1   # evidence extractor calls per paper; with >1, only entries found in EVERY run are kept (agreement
                    # filter, user Oct 23). Default 1 since the checker pipeline (Oct 26) replaces the vote.

def _ev_key(e):
    return (e.get("evidence_type"), e.get("evidence_note"), (e.get("waveband") or "").strip().lower())

def evidence_consensus(runs):
    """Agreement filter over several evidence runs (each a list of {source_nickname, evidence}). An entry is kept only
    if an entry with the same type, note and waveband (case-insensitive) is found for the same source in EVERY run;
    the first run's entry (and quote) is kept. Sources are matched by normalised name (substring either way).
    -> (consensus sources, dropped entries [{source_nickname, run, found_in_runs, evidence_type, evidence_note,
    waveband, quote}])."""
    import copy
    norm = lambda x: re.sub(r"[^a-z0-9]", "", (x or "").lower())
    n = len(runs)
    if n <= 1: return copy.deepcopy(runs[0] if runs else []), []
    def match(name, srcs):
        a = norm(name)
        hit = [s for s in srcs if norm(s.get("source_nickname")) and a and (norm(s["source_nickname"]) in a or a in norm(s["source_nickname"]))]
        return hit[0] if hit else (srcs[0] if len(srcs) == 1 and len(runs[0]) == 1 else None)
    out, dropped, seen = [], [], set()
    # every source appearing in any run; the first run with it defines its name
    names = []
    if all(len(srcs) == 1 for srcs in runs): names = [runs[0][0].get("source_nickname")]   # single-source paper
    for srcs in ([] if names else runs):
        for s in srcs:
            if not any(norm(s.get("source_nickname")) == norm(x) for x in names): names.append(s.get("source_nickname"))
    for name in names:
        per = [match(name, srcs) for srcs in runs]
        keysets = [{_ev_key(e) for e in (p_ or {}).get("evidence", [])} for p_ in per]
        common = set.intersection(*keysets) if keysets else set()
        base = next((p_ for p_ in per if p_), None)
        kept = [copy.deepcopy(e) for e in (base or {}).get("evidence", []) if _ev_key(e) in common]
        if kept or any(per): out.append({"source_nickname": name, "evidence": kept})
        for ri, p_ in enumerate(per):
            for e in (p_ or {}).get("evidence", []):
                k = _ev_key(e)
                if k in common or (name, k) in seen: continue
                seen.add((name, k))
                dropped.append(dict(source_nickname=name, run=ri + 1,
                                    found_in_runs=[j + 1 for j, ks in enumerate(keysets) if k in ks],
                                    evidence_type=e.get("evidence_type"), evidence_note=e.get("evidence_note"),
                                    waveband=e.get("waveband"), quote=e.get("quote", "")))
    return out, dropped

def evidence_dropped_log(ext):
    """review_log entries for evidence removed by the agreement filter, attached to the models of that source."""
    norm = lambda x: re.sub(r"[^a-z0-9]", "", (x or "").lower())
    models = ext.get("models", []); log = []
    for d in ext.get("evidence_dropped", []):
        a = norm(d["source_nickname"])
        idx = [i for i, m in enumerate(models) if a and (lambda b: b and (a in b or b in a))(norm(m.get("source_nickname") or ext.get("source_nickname")))]
        if not idx and len(models) == 1: idx = [0]
        for i in (idx or [None]):
            log.append(dict(model=i, item="evidence[-]", action="evidence_not_in_all_runs",
                            old_value=f"{d['evidence_type']} / {d['evidence_note']} / {d['waveband']}",
                            new_value="dropped", applied=True,
                            reason=(f"found in evidence run(s) {d['found_in_runs']} of {ext.get('evidence_runs_n', EVIDENCE_RUNS)}; "
                                    f"kept only entries found in every run. Quote: {d['quote']}")))
    return log

def assign_evidence(ext):
    """Copy per-source evidence onto every model of that source (single-source papers: all models)."""
    import copy
    srcs = ext.get("evidence_sources") or []
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
    for m in ext.get("models", []):
        name = norm(m.get("source_nickname") or ext.get("source_nickname"))
        hit = [s for s in srcs if norm(s.get("source_nickname")) and name and
               (norm(s["source_nickname"]) in name or name in norm(s["source_nickname"]))]
        if not hit and len(srcs) == 1: hit = srcs
        m["evidence"] = copy.deepcopy(hit[0]["evidence"]) if hit else []
        if not hit and srcs:
            m["extraction_notes"] = ((m.get("extraction_notes") or "") + " [Evidence step: no evidence list matched this source name]").strip()
    return ext

def _decode_fields(m):
    """Decode values the model returned as JSON strings (e.g. '{"value": 9.7, ...}') into objects; drop 'null' strings."""
    if isinstance(m, str):
        try: m = json.loads(m)
        except Exception: return m
    if not isinstance(m, dict): return m
    out = {}
    for k, v in m.items():
        if isinstance(v, str) and v.strip()[:1] in "{[":
            try: v = json.loads(v)
            except Exception: pass
        elif isinstance(v, str) and v.strip().lower() in ("null", "none"): v = None
        out[k] = v
    return out

def extract(tex, tpl, model=None, max_tokens=32000, evidence_model=None):
    """Value extraction and evidence classification run as two separate model calls, in parallel.
    Evidence is stored per source in ext['evidence_sources'] and copied onto models by assign_evidence()."""
    tool = build_tool(tpl)
    global _VOCAB_TXT
    vocab = "\n".join(f"  {t}: {'; '.join(n)}" for t, n in tpl["vocab"].items()); _VOCAB_TXT = vocab
    sys = SYSTEM.format(field_rules=_field_rules(), vocab=vocab, wavebands=", ".join(tpl["wavebands"]), max_models=FIRST_PASS_MAX_MODELS)
    value_req = dict(prompt="PAPER TEXT (LaTeX):\n\n" + tex, system=sys, tools=[tool],
                     tool_choice={"type": "tool", "name": tool["name"]},
                     model=_values_model(model), max_tokens=max_tokens)
    _t0 = time.time()
    outs = _llm_tool([value_req] + [_evidence_request(tex, tpl, evidence_model) for _ in range(max(1, EVIDENCE_RUNS))])
    _t1 = time.time()
    r, r_evs = outs[0], outs[1:]
    if r.get("error"): raise RuntimeError(f"value extraction failed: {r['error']}")
    parsed = [_parse_evidence(x) for x in r_evs]
    ok_runs = [srcs for srcs, err in parsed if not err]
    ev_err = "; ".join(err for _, err in parsed if err)
    ev_sources, ev_dropped = evidence_consensus(ok_runs)
    if len(ok_runs) < len(r_evs) and ok_runs:
        ev_err = (ev_err + f" [only {len(ok_runs)} of {len(r_evs)} evidence runs succeeded; agreement filter used those]").strip()
    tu = r.get("tool_use"); tu = tu[0] if isinstance(tu, list) else tu
    ext = _unwrap(tu["input"] if "input" in tu else tu, ["models"])
    ext["models"] = _coerce_list(ext.get("models", []), "models")
    mkeys = set(tool["input_schema"]["properties"]["models"]["items"].get("properties", {}))
    if not ext["models"]:                        # keep the raw tool input for diagnosis
        ext["_raw_value_output"] = json.dumps(tu.get("input", tu) if isinstance(tu, dict) else tu, default=str)[:30000]
    if not ext["models"] and (mkeys - {"source_nickname", "redshift"}) & set(ext):   # model fields at top level, models[] empty
        fix = dict(value_req, system=sys + "\n\nIMPORTANT: put EVERY model's parameters inside the `models` array "
                   "(one item per distinct model); do not place model fields at the top level and never leave `models` empty.")
        r2 = _llm_tool(fix); tu2 = r2.get("tool_use"); tu2 = tu2[0] if isinstance(tu2, list) else tu2
        if not r2.get("error") and tu2:
            e2 = _unwrap(tu2["input"] if "input" in tu2 else tu2, ["models"]); e2["models"] = _coerce_list(e2.get("models", []), "models")
            if e2["models"]: ext, r = e2, r2
        if not ext["models"]:
            ext["models"] = [{k: ext.pop(k) for k in list(ext) if k in mkeys and k not in _HEADER_KEYS}]
            ext["extraction_notes_pipeline"] = "model fields were returned at top level; gathered into one model"
    for k in [k for k in list(ext) if k in mkeys and k not in _HEADER_KEYS]: ext.pop(k)   # top-level name/redshift are kept
    ext["models"] = [_decode_fields(m) for m in ext["models"] if isinstance(m, dict) or isinstance(_decode_fields(m), dict)]
    def _hollow(ms):   # no model carries any quantity value or observed period (malformed tool output)
        return not any(isinstance(m.get(k), dict) and m[k].get("value") is not None for m in ms for k in QFIELDS) and \
               not any(m.get("observed_periods") for m in ms)
    for attempt in range(2):                     # re-ask from scratch when the output carried no values at all
        if not _hollow(ext["models"]): break
        r3 = _llm_tool(dict(value_req, system=sys + "\n\nIMPORTANT: the previous answer contained no parameter values. Put EVERY "
                            "model's parameters inside the `models` array (one item per model), with each quantity field filled "
                            "as an object (value, unit, error_type, quote, note); use value=null only for quantities the paper does not state."))
        tu3 = r3.get("tool_use"); tu3 = tu3[0] if isinstance(tu3, list) else tu3
        if r3.get("error") or not tu3: continue
        e3 = _unwrap(tu3["input"] if "input" in tu3 else tu3, ["models"])
        e3["models"] = [_decode_fields(m) for m in _coerce_list(e3.get("models", []), "models")]
        e3["models"] = [m for m in e3["models"] if isinstance(m, dict)]
        if not e3["models"] and set(QFIELDS) & set(e3):
            e3["models"] = [_decode_fields({k: e3.pop(k) for k in list(e3) if k in mkeys and k not in _HEADER_KEYS})]
        if e3["models"] and not _hollow(e3["models"]):
            for k in [k for k in list(e3) if k in mkeys and k not in _HEADER_KEYS]: e3.pop(k)
            ext = e3; r = r3; ext["extraction_notes_pipeline"] = f"first value extraction returned no values; re-asked ({attempt + 1})"
    if _hollow(ext["models"]):
        ext["value_extraction_failed"] = True
    ext["evidence_sources"] = ev_sources
    ext["evidence_runs"] = ok_runs; ext["evidence_runs_n"] = len(ok_runs); ext["evidence_dropped"] = ev_dropped
    ext["_models_used"] = dict(values=_values_model(model), evidence=_evidence_model(evidence_model))
    if ev_err: ext["evidence_error"] = ev_err
    _t2 = time.time(); ext = _fill_header(ext, tex, model)
    ext["_timing"] = dict(values_and_evidence=round(_t1 - _t0, 1), value_retries=round(_t2 - _t1, 1), header=round(time.time() - _t2, 1))
    return ext, r

RECOVERY_BATCH = 8          # models per targeted-extraction / review call (no limit on the number of models per paper)
FIRST_PASS_MAX_MODELS = 8   # first extraction records at most this many models; the rest come from the targeted passes

def missing_models(rev, n_models):
    """Models the reviewer's paper_models list marks as not covered by the extraction (extracted_model_index -1/null)."""
    out = []
    for pm in _coerce_list((rev or {}).get("paper_models", []), "paper_models"):
        if not isinstance(pm, dict): continue
        i = pm.get("extracted_model_index")
        if i is None or (isinstance(i, int) and i < 0): out.append(pm)
    return out

def extract_missing_models(tex, tpl, ext, wanted, model=None, max_tokens=32000):
    """Targeted value extraction for models the review found missing, RECOVERY_BATCH models per call, all calls in
    parallel (no limit on the number of models). Returns (models, errors); each model has source_nickname set."""
    tool = build_tool(tpl)
    sys0 = SYSTEM.format(field_rules=_field_rules(), vocab=_VOCAB_TXT, wavebands=", ".join(tpl["wavebands"]), max_models=FIRST_PASS_MAX_MODELS)
    have = "\n".join(f"  [{i}] source={m.get('source_nickname') or ext.get('source_nickname')!r} label={m.get('model_label')!r}"
                     for i, m in enumerate(ext.get("models", [])))
    chunks = [wanted[i:i + RECOVERY_BATCH] for i in range(0, len(wanted), RECOVERY_BATCH)]
    reqs = []
    for ch in chunks:
        want = "\n".join(f"  - source={w.get('source_nickname')!r} label={w.get('model_label')!r}: {w.get('description', '')}"
                         + (f" (quote: {w.get('quote')!r})" if w.get("quote") else "") for w in ch)
        sys = sys0 + ("\n\nTARGETED PASS: an earlier extraction of this paper already recorded these models:\n" + have +
                      "\nA reviewer found that the paper also proposes further models. Extract ONLY the following missing "
                      "model(s), one item in `models` per model, following all rules above (other missing models are handled "
                      "in separate passes):\n" + want +
                      "\nSet source_nickname (and redshift, if stated) on every model. If on inspection a listed item is not a "
                      "distinct binary parameter set proposed or adopted by the paper, omit it (an empty `models` list is allowed).")
        reqs.append(dict(prompt="PAPER TEXT (LaTeX):\n\n" + tex, system=sys, tools=[tool],
                         tool_choice={"type": "tool", "name": tool["name"]}, model=_values_model(model), max_tokens=max_tokens))
    out, errs = [], []
    for ch, r in zip(chunks, _llm_tool(reqs, max_concurrency=8)):
        if not isinstance(r, dict) or r.get("error"):
            errs.append(f"targeted model extraction failed for {[w.get('model_label') for w in ch]}: {r.get('error') if isinstance(r, dict) else r}")
            continue
        tu = r.get("tool_use"); tu = tu[0] if isinstance(tu, list) else tu
        e = _unwrap((tu or {}).get("input", tu or {}), ["models"])
        ms = [m for m in (_decode_fields(m) for m in _coerce_list(e.get("models", []), "models")) if isinstance(m, dict)]
        if r.get("stop_reason") == "max_tokens":
            errs.append(f"targeted pass hit max_tokens for {[w.get('model_label') for w in ch]} (output may be incomplete)")
        for m in ms:
            if not m.get("source_nickname"): m["source_nickname"] = e.get("source_nickname") or ext.get("source_nickname")
        out += ms
    return out, errs

# ----------------------------------------------------------------- evidence checkers (user design, Oct 26)
# Pipeline: evidence extractor (Opus) -> type-by-type OMISSION check (adds missed entries) -> UNION ->
# FALSE-POSITIVE check of every entry (keep / drop / relabel) -> assign to models. Every addition, drop and relabel
# goes to the review sheet. Checker models are configurable (user: Sonnet 5.5 for now; Opus 5.5 if unsatisfied).
OMISSION_MODEL = "sonnet-5-5"
FP_MODEL = "sonnet-5-5"
REVIEW_MODEL = "opus-5-5"       # value-extraction review model, used only if VALUE_REVIEW is True
VALUE_REVIEW = False            # user, Oct 27: value reviewer scrapped (also disables the review's model listing / recovery)
OMISSION_CHECK = False          # user, Oct 27: omission checker removed (the false-positive checker still runs)
VALUES_MODEL = "opus-5-5"       # user, Oct 27: value extraction on Opus 5.5 (explicit model= to run() still overrides)
_HEADER_KEYS = ("source_nickname", "redshift")   # top-level AND per-model keys: never popped from the top level
HEADER_REASK = True             # user, Oct 27: Haiku re-ask only as fallback when the value extraction omits name/redshift
HEADER_MODEL = "haiku-5-5"      # user, Oct 27: name/redshift re-ask on Haiku 5.5
EVIDENCE_CHECKERS = True        # False -> previous behaviour (extractor only, plus the EVIDENCE_RUNS agreement filter)
FP_CONTEXT_SENTENCES = 2        # sentences before and after the quote given to the false-positive checker

def _resolve_model(substr):
    """Available model id containing substr (shortest match); falls back to _main_model()."""
    try:
        hits = [m for m in host.list_models() if substr and substr in m]
        if hits: return sorted(hits, key=len)[0]
    except Exception:
        pass
    return _main_model()

def _evidence_defs(tpl):
    return EVIDENCE_SYSTEM.format(guide=_evidence_guide(tpl), wavebands=", ".join(tpl["wavebands"]), rules=EVIDENCE_RULES)

def _ev_listing(sources):
    lines = []
    for s in sources or []:
        lines.append(f"SOURCE {s.get('source_nickname')!r}:")
        for e in s.get("evidence", []):
            lines.append(f"  - {e.get('evidence_type')} / {e.get('evidence_note')} / {e.get('waveband')} | quote: {(e.get('quote') or '')[:200]!r}")
        if not s.get("evidence"): lines.append("  (none)")
    return "\n".join(lines) or "(no evidence extracted)"

OMISSION_TOOL = {"name": "record_omissions", "description": "Evidence entries missing from the extracted list, found by a type-by-type sweep.",
    "input_schema": {"type": "object", "properties": {
        "type_sweep": {"type": "array", "description": "One item per evidence TYPE, in the order listed, recording the search.",
            "items": {"type": "object", "properties": {"evidence_type": {"type": "string"},
                      "result": {"type": "string", "description": "One line: what the paper says for this type, and whether anything is missing."}},
                      "required": ["evidence_type", "result"]}},
        "sources": {"type": "array", "description": "ONLY new entries not already in the list (empty if none).",
            "items": {"type": "object", "properties": {"source_nickname": {"type": "string"}, "evidence": {"type": "array", "items": _EVID}},
                      "required": ["source_nickname", "evidence"]}}},
        "required": ["type_sweep", "sources"]}}

def _omission_request(tex, tpl, sources, max_tokens=16000):
    sys = _evidence_defs(tpl) + """

OMISSION CHECK. You are given the paper and the evidence list another reader already extracted. Find evidence that list
MISSED. Go through EVERY evidence type above, one at a time and in order, and search the whole paper (text, captions,
tables) for passages supporting any note of that type for each source. Record the search in type_sweep (one line per
type). Report in `sources` ONLY entries that are not already in the list (an entry is already covered if the list has
the same source, type, note and waveband). Apply exactly the same definitions and rules as above; do not add entries
that the rules exclude. An empty `sources` list is a valid answer."""
    return dict(prompt="PAPER TEXT:\n\n" + tex + "\n\nEVIDENCE ALREADY EXTRACTED:\n" + _ev_listing(sources), system=sys,
                tools=[OMISSION_TOOL], tool_choice={"type": "tool", "name": "record_omissions"},
                model=_resolve_model(OMISSION_MODEL), max_tokens=max_tokens)

def _parse_omission(r):
    srcs, err = _parse_evidence(r)
    tu = (r or {}).get("tool_use") if isinstance(r, dict) else None; tu = tu[0] if isinstance(tu, list) and tu else tu
    sweep = _coerce_list(_unwrap((tu or {}).get("input", tu or {}), ["type_sweep"]).get("type_sweep", []), "type_sweep") if tu else []
    return srcs, sweep, err

def evidence_union(base, extra):
    """Union of two per-source evidence lists; entries tagged with _origin. Duplicate (source, type, note, waveband)
    keys keep the base entry. Sources are matched by normalised name (substring either way; single-source lists match)."""
    import copy
    norm = lambda x: re.sub(r"[^a-z0-9]", "", (x or "").lower())
    out = copy.deepcopy(base or [])
    for s in out:
        for e in s.get("evidence", []): e.setdefault("_origin", "extractor")
    for s in extra or []:
        a = norm(s.get("source_nickname"))
        hit = [t for t in out if norm(t.get("source_nickname")) and a and (norm(t["source_nickname"]) in a or a in norm(t["source_nickname"]))]
        if not hit and len(out) == 1 and len(extra) == 1: hit = out
        if not hit: out.append({"source_nickname": s.get("source_nickname"), "evidence": []}); hit = out[-1:]
        have = {_ev_key(e) for e in hit[0]["evidence"]}
        for e in s.get("evidence", []):
            if _ev_key(e) in have: continue
            have.add(_ev_key(e)); hit[0]["evidence"].append(dict(copy.deepcopy(e), _origin="omission_check"))
    return out

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\[(\\$])")

def _quote_window(quote, tex, n=FP_CONTEXT_SENTENCES):
    """The quote's position in the text, widened to n sentences before and after. -> (window text, found?)."""
    q = (quote or "").strip()
    if not q or not tex: return "", False
    pos = tex.find(q[:120])
    if pos < 0:                                  # whitespace/LaTeX-tolerant search on the first ~10 words
        words = [re.escape(w) for w in re.findall(r"[A-Za-z0-9]{3,}", q)[:10]]
        m = re.search(r"\W+(?:\S*\W+){0,3}?".join(words), tex) if len(words) >= 4 else None
        pos = m.start() if m else -1
    if pos < 0: return "", False
    lo, hi = max(0, pos - 2500), min(len(tex), pos + len(q) + 2500)
    chunk = tex[lo:hi]; rel = pos - lo
    bounds = [0] + [m.end() for m in _SENT_SPLIT.finditer(chunk)] + [len(chunk)]
    i = max(k for k in range(len(bounds) - 1) if bounds[k] <= rel)
    j = min([k for k in range(len(bounds)) if bounds[k] >= rel + len(q)] or [len(bounds) - 1])
    return chunk[bounds[max(0, i - n)]: bounds[min(len(bounds) - 1, j + n)]].strip(), True

FP_TOOL = {"name": "record_fp_verdicts", "description": "One verdict per candidate evidence entry.",
    "input_schema": {"type": "object", "properties": {"verdicts": {"type": "array", "items": {"type": "object", "properties": {
        "index": {"type": "integer"},
        "about_this_source": {"type": "boolean"},
        "is_observation_of_source": {"type": "boolean", "description": "An observation or measurement of this source (not a class-level or theoretical statement, not a prediction)."},
        "used_for_binary_case": {"type": "boolean", "description": "The paper presents it as supporting or consistent with the binary model it proposes or considers for THIS source (not solely an alternative scenario; not merely evidence for binaries in general)."},
        "best_label_ok": {"type": "boolean", "description": "Type, note and waveband are the best fit under the definitions."},
        "verdict": {"type": "string", "enum": ["keep", "drop", "relabel"]},
        "new_evidence_type": {"type": "string"}, "new_evidence_note": {"type": "string"}, "new_waveband": {"type": "string"},
        "reason": {"type": "string"}},
        "required": ["index", "about_this_source", "is_observation_of_source", "used_for_binary_case", "best_label_ok", "verdict", "reason"]}}},
        "required": ["verdicts"]}}

FP_ALLOW_RELABEL = False    # user experiment (Oct 28): checker only keeps/drops; True restores keep/drop/relabel (baseline)
HARD_RULE_DROP = True       # drop-only mode: code drops entries whose label breaks a hard rule (_hard_rule_violation), logged

FP_TOOL_DROP_ONLY = {"name": "record_fp_verdicts", "description": "One keep/drop verdict per candidate evidence entry.",
    "input_schema": {"type": "object", "properties": {"verdicts": {"type": "array", "items": {"type": "object", "properties": {
        "index": {"type": "integer"},
        "about_this_source": {"type": "boolean"},
        "is_observation_of_source": {"type": "boolean", "description": "An observation or measurement of this source (not a class-level or theoretical statement, not a prediction)."},
        "used_for_binary_case": {"type": "boolean", "description": "The paper presents it as supporting or consistent with the binary model it proposes or considers for THIS source (not solely an alternative scenario; not merely evidence for binaries in general)."},
        "verdict": {"type": "string", "enum": ["keep", "drop"]},
        "reason": {"type": "string"}},
        "required": ["index", "about_this_source", "is_observation_of_source", "used_for_binary_case", "verdict", "reason"]}}},
        "required": ["verdicts"]}}

_FP_INTRO = """FALSE-POSITIVE CHECK. You are given the paper and a list of candidate evidence entries, each with its quote and the
surrounding context. Judge EACH entry on its own under the definitions and rules above, using the context first and the
full paper for anything the context does not settle (which source it concerns, the instrument/data band, whether the
paper uses it for the binary case, single- vs multi-epoch). For each entry answer the checklist, then give a verdict:"""
_FP_DROP_DEF = """- drop: not evidence under the rules (not about this source; not an observation or measurement of it; not presented as
  supporting or consistent with the binary interpretation; solely supports a non-binary scenario; not supported by the
  text)."""
_FP_VERDICTS_RELABEL = """- keep: genuine evidence, correctly labelled.
""" + _FP_DROP_DEF + """
- relabel: genuine evidence but another allowed type / note / waveband fits better; give the new values (allowed
  values only)."""
_FP_VERDICTS_DROP_ONLY = """Your ONLY task is to remove extraneous entries: entries that are not genuine evidence for this source's binary
model. You do not judge or change labels; the type / note / waveband of each entry is fixed.
- keep: genuine evidence for this source's binary model, whatever its label. Never drop a genuine piece of evidence
  because another type, note or waveband might fit it better.
""" + _FP_DROP_DEF
_FP_CRITERIA = """Additional drop criteria:
- The entry must be evidence for the binary model(s) the paper proposes or considers for THIS source. A feature that
  could support a binary in general, but that the paper does not apply to its proposed binary model (or attributes to
  another cause), is a false positive: drop it.
- Be conservative with speculative evidence, toy models and hypotheticals. Drop an entry when the feature exists only in
  a toy model, simulation or hypothetical scenario rather than in this source's data, or when the paper only speculates
  that a feature indicates a binary or merger, unless the paper clearly relies on it for its binary case."""
_FP_BANDS_RELABEL = """- Wavebands: a passage that reports the feature in two or more observer bands gets one entry per band (one waveband per
  entry). An entry for one of those bands is not a duplicate of the entry for another band: do not drop it for that
  reason, and do not move its waveband to another band the same passage also covers."""
_FP_BANDS_DROP_ONLY = """- Wavebands: a passage that reports the feature in two or more observer bands gets one entry per band (one waveband per
  entry). An entry for one of those bands is not a duplicate of the entry for another band: do not drop it for that
  reason. Judge the band entries of the same passage together and give them the same verdict, unless the paper itself
  treats the feature differently in different bands.
- When you are unsure whether an entry is genuine evidence, keep it. Drop only entries that clearly meet a drop
  criterion (the speculative, toy-model and hypothetical cases above count as clear)."""
_FP_OUTRO = """Do not drop an entry merely because it is brief or because the paper also discusses alternatives. Give one verdict for
every index."""

def _fp_request(tex, tpl, sources, max_tokens=16000):
    items, flat = [], []
    for s in sources or []:
        for e in s.get("evidence", []):
            win, found = _quote_window(e.get("quote"), tex)
            flat.append((s, e))
            items.append(f"[{len(flat) - 1}] SOURCE {s.get('source_nickname')!r} | {e.get('evidence_type')} / {e.get('evidence_note')} / "
                         f"{e.get('waveband')} | from_this_paper={e.get('from_this_paper')}\n    QUOTE: {(e.get('quote') or '')!r}\n    "
                         + (f"CONTEXT: {win!r}" if found else "CONTEXT: (quote NOT FOUND verbatim in the text: check that the evidence exists)"))
    if FP_ALLOW_RELABEL:
        body, bands, tool = _FP_VERDICTS_RELABEL, _FP_BANDS_RELABEL, FP_TOOL
    else:
        body, bands, tool = _FP_VERDICTS_DROP_ONLY, _FP_BANDS_DROP_ONLY, FP_TOOL_DROP_ONLY
    sys = _evidence_defs(tpl) + "\n\n" + "\n".join([_FP_INTRO, body, _FP_CRITERIA, bands, _FP_OUTRO])
    return dict(prompt="PAPER TEXT:\n\n" + tex + "\n\nCANDIDATE EVIDENCE ENTRIES:\n" + "\n".join(items), system=sys,
                tools=[tool], tool_choice={"type": "tool", "name": "record_fp_verdicts"},
                model=_resolve_model(FP_MODEL), max_tokens=max_tokens), flat

_FORBIDDEN_LINE_RE = re.compile(r"forbidden|narrow[- ]line|\[\s*(?:O|N|S|Ne|Ar|Fe|C|Ca)\s*(?:I{1,3}|IV|V|VI{0,3})\s*\]", re.I)

def _hard_rule_violation(e):
    """Deterministic check of hard labelling rules (used when the false-positive checker's relabel is blocked because the
    target label already exists). -> reason string or None. Rule: forbidden lines never arise in the BLR, so a broad-line
    note whose quote concerns forbidden / narrow lines and never mentions broad lines is invalid. (The burst rule is
    enforced later by fix_evidence.)"""
    note, q = str(e.get("evidence_note") or ""), str(e.get("quote") or "")
    if re.search(r"BLR|broad[- ]line", note, re.I) and _FORBIDDEN_LINE_RE.search(q) and not re.search(r"\bbroad", q, re.I):
        return "forbidden/narrow-line feature under a broad-line note; forbidden lines are never BLR"
    return None

def apply_fp(sources, flat, r, tpl):
    """Apply false-positive verdicts. -> (sources, log, verdicts). Unparseable output keeps every entry (logged)."""
    log = []
    tu = r.get("tool_use") if isinstance(r, dict) else None; tu = tu[0] if isinstance(tu, list) and tu else tu
    if not isinstance(r, dict) or r.get("error") or not tu:
        log.append(dict(model=None, item="evidence", action="fp_check", applied=False,
                        reason=f"false-positive check failed ({(r or {}).get('error') if isinstance(r, dict) else r}); all entries kept"))
        return sources, log, []
    vs = _coerce_list(_unwrap(tu.get("input", tu), ["verdicts"]).get("verdicts", []), "verdicts")
    byi = {v.get("index"): v for v in vs if isinstance(v, dict) and isinstance(v.get("index"), int)}
    drop = set()
    for i, (s, e) in enumerate(flat):
        v = byi.get(i)
        lbl = f"{e.get('evidence_type')} / {e.get('evidence_note')} / {e.get('waveband')}"
        org = e.get("_origin", "extractor")
        if v is None:
            log.append(dict(model=None, item="evidence", action="fp_no_verdict", old_value=lbl, applied=False,
                            reason=f"checker gave no verdict; kept (origin: {org})")); continue
        why = (v.get("reason") or "") + f" [source: {s.get('source_nickname')}; origin: {org}; quote: {(e.get('quote') or '')[:200]}]"
        if v.get("verdict") == "drop":
            drop.add(id(e)); log.append(dict(model=None, item="evidence[-]", action="fp_drop", old_value=lbl, new_value="dropped", applied=True, reason=why))
        elif v.get("verdict") == "relabel" and not FP_ALLOW_RELABEL:     # drop-only mode: a relabel verdict counts as keep
            log.append(dict(model=None, item="evidence", action="fp_relabel_ignored", old_value=lbl, applied=False,
                            reason=why + " [drop-only checker: relabel verdicts are not applied; entry kept]"))
        elif v.get("verdict") == "relabel" and (v.get("new_evidence_type") or e.get("evidence_type"),
                                                 v.get("new_evidence_note") or e.get("evidence_note"),
                                                 v.get("new_waveband") or e.get("waveband")) == (e.get("evidence_type"), e.get("evidence_note"), e.get("waveband")):
            if org == "omission_check":       # 'relabel' to the same label = keep
                log.append(dict(model=None, item="evidence[+]", action="added_by_omission_check", new_value=lbl, applied=True, reason=why))
        elif v.get("verdict") == "relabel":
            t = v.get("new_evidence_type") or e.get("evidence_type"); n = v.get("new_evidence_note") or e.get("evidence_note")
            w = v.get("new_waveband") or e.get("waveband")
            dup = any(o is not e and _ev_key(o) == _ev_key(dict(evidence_type=t, evidence_note=n, waveband=w))
                      for o in s.get("evidence", []) if id(o) not in drop)
            bad = _hard_rule_violation(e) if dup else None
            if dup and bad:                      # blocked relabel, but the original label breaks a hard rule: drop it
                drop.add(id(e))
                log.append(dict(model=None, item="evidence[-]", action="fp_drop", old_value=lbl, new_value="dropped", applied=True,
                                reason=why + f" [relabel to existing label {t} / {n} / {w}: original label breaks a hard rule ({bad}); "
                                             "entry dropped instead of kept]"))
            elif dup:
                log.append(dict(model=None, item="evidence", action="fp_relabel", old_value=lbl, new_value=f"{t} / {n} / {w}", applied=False,
                                reason=why + " [NOT APPLIED: the new label already exists for this source; relabelling would delete this "
                                             "entry as a duplicate, so the original label is kept]"))
            elif n in tpl["vocab"].get(t, []) and w in tpl["wavebands"]:
                e["evidence_type"], e["evidence_note"], e["waveband"] = t, n, w
                log.append(dict(model=None, item="evidence", action="fp_relabel", old_value=lbl, new_value=f"{t} / {n} / {w}", applied=True, reason=why))
            else:
                log.append(dict(model=None, item="evidence", action="fp_relabel", old_value=lbl, new_value=f"{t} / {n} / {w}", applied=False,
                                reason=why + " [NOT APPLIED: not an allowed type/note/waveband; entry kept as is]"))
        elif org == "omission_check":
            log.append(dict(model=None, item="evidence[+]", action="added_by_omission_check", new_value=lbl, applied=True, reason=why))
    if not FP_ALLOW_RELABEL and HARD_RULE_DROP:  # no relabelling: an entry whose label breaks a hard rule is dropped by code
        for s, e in flat:
            bad = _hard_rule_violation(e) if id(e) not in drop else None
            if bad:
                drop.add(id(e))
                log.append(dict(model=None, item="evidence[-]", action="hard_rule_drop", applied=True,
                                old_value=f"{e.get('evidence_type')} / {e.get('evidence_note')} / {e.get('waveband')}", new_value="dropped",
                                reason=f"label breaks a hard rule ({bad}); the drop-only checker cannot relabel [source: "
                                       f"{s.get('source_nickname')}; quote: {(e.get('quote') or '')[:200]}]"))
    for s in sources:
        s["evidence"] = [e for e in s.get("evidence", []) if id(e) not in drop]
    return sources, log, vs

def check_evidence(tex, tpl, ext, review_req=None):
    """Omission check (in parallel with the value review request, if given) -> union -> false-positive check.
    Updates ext['evidence_sources'] and stores the intermediate lists. -> (review result or None, log)."""
    import copy
    base = ext.get("evidence_sources") or []
    reqs = ([_omission_request(tex, tpl, base)] if OMISSION_CHECK else []) + ([review_req] if review_req else [])
    _t0 = time.time()
    outs = _llm_tool(reqs, max_concurrency=4) if reqs else []
    _t1 = time.time()
    om = outs[0] if OMISSION_CHECK else None
    rv = outs[-1] if review_req else None
    add, sweep, err = _parse_omission(om) if OMISSION_CHECK else ([], [], "")
    log = []
    if err: log.append(dict(model=None, item="evidence", action="omission_check", applied=False, reason=f"omission check failed: {err}"))
    union = evidence_union(base, add)
    fp_req, flat = _fp_request(tex, tpl, union)
    fixed, lg, verdicts = apply_fp(union, flat, _llm_tool(fp_req) if flat else {"tool_use": {"input": {"verdicts": []}}}, tpl)
    ext.setdefault("_timing", {}).update(omission_and_review=round(_t1 - _t0, 1), false_positive=round(time.time() - _t1, 1))
    for s in fixed:
        for e in s.get("evidence", []): e.pop("_split_from", None)
    ext.update(evidence_extractor=copy.deepcopy(base), evidence_omission_added=add, evidence_type_sweep=sweep,
               evidence_fp_verdicts=verdicts, evidence_sources=fixed,
               _checker_models=dict(omission=_resolve_model(OMISSION_MODEL), false_positive=_resolve_model(FP_MODEL)))
    return rv, log + lg

# ----------------------------------------------------------------- review pass
REVIEW_SYSTEM = """You are a strict referee auditing an automated extraction of SMBHB parameters for the BOBcat
catalogue. You get the paper text and the extraction JSON. For EVERY non-null quantity (each QFIELD and each
observed_periods entry) decide whether it is correct under these field definitions:
{field_rules}
- Change a frame only if the text explicitly labels that value's frame.
Also check numbers, errors, units, frames and error_type against the text.
List observed periodicities the model relies on (including ones measured in other papers) that the
extraction missed (missing_observed_periods), and use 'fix_cited_from' (new_value = reference) where a value the
paper attributes to another work lacks cited_from.
Return one verdict per checked item: action='clear' removes it; 'fix_error_type'/'fix_frame' set that attribute
to new_value (must be a valid option); 'keep' if correct. Then, for each model, return revised_caveats: the
extraction's Caveats text with MINIMAL edits only - keep its wording, delete statements that are not from THIS
paper or do not weaken the claim that the source is a true SMBHB (e.g. catalogue bookkeeping), correct factual
errors, and trim to under 750 characters if needed. Do not rewrite from scratch.
Finally, list EVERY distinct model in the paper (paper_models), independently of how the extraction grouped them: each
self-consistent BINARY parameter set the authors propose or adopt, for every source the paper covers (e.g. alternative
binary scenarios for one source, or one model per binary/dual candidate in a multi-candidate paper). Include binary
models whether or not they meet the BOBcat definition (code checks that). Do NOT list non-binary alternatives, parameter
sets from other papers that this paper only cites for comparison or rejects, or mere variants of one parameter set (the
same value quoted with different precision, error ranges or derived quantities of the same scenario). List a source only
if the paper states at least one binary parameter for THAT source (BH mass(es), mass ratio, separation, angular
separation, semi-major axis, orbital or observed period, eccentricity, inclination); candidates that only appear in a
sample table or list without such parameters are not models. For each, set
extracted_model_index to the 0-based index of the extraction model that covers it, or -1 if no extraction model does."""


REVIEW_TOOL = {"name": "record_review", "description": "Verdicts on extracted items.",
    "input_schema": {"type": "object", "properties": {
        "verdicts": {"type": "array", "items": {"type": "object", "properties": {
            "model_index": {"type": "integer"},
            "item": {"type": "string", "description": "QFIELD name or 'observed_periods[i]'."},
            "action": {"type": "string", "enum": ["keep", "clear", "fix_error_type", "fix_frame", "fix_cited_from"]},
            "new_value": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["model_index", "item", "action", "reason"]}},
        "missing_observed_periods": {"type": "array", "description": "Observed periodicities the model relies on that the extraction omitted.",
            "items": {"type": "object", "properties": {"model_index": {"type": "integer"}, **_PER["properties"]},
                      "required": ["model_index"] + _PER["required"]}},
        "revised_caveats": {"type": "array", "items": {"type": "object", "properties": {
            "model_index": {"type": "integer"}, "caveats": {"type": "string"}},
            "required": ["model_index", "caveats"]}},
        "paper_models": {"type": "array", "description": "Every distinct binary model (parameter set) the paper proposes or adopts, per source.",
            "items": {"type": "object", "properties": {
                "source_nickname": {"type": "string"}, "model_label": {"type": "string"},
                "description": {"type": "string", "description": "One line: scenario and key parameters (e.g. masses, separation)."},
                "quote": {"type": "string", "description": "Short verbatim quote stating the model's key parameter(s)."},
                "extracted_model_index": {"type": "integer", "description": "0-based index of the extraction model covering it; -1 if none."}},
                "required": ["source_nickname", "model_label", "description", "extracted_model_index"]}}},
        "required": ["verdicts", "revised_caveats", "paper_models"]}}

_VOCAB_TXT = ""

def _values_only(ext):
    """Copy of the extraction without evidence (the value review does not audit evidence)."""
    import copy
    e = copy.deepcopy(ext); e.pop("evidence_sources", None)
    for m in e.get("models", []): m.pop("evidence", None)
    return e

def _review_request(tex, ext, model=None, max_tokens=32000):
    return dict(prompt="PAPER TEXT:\n\n" + tex + "\n\nEXTRACTION JSON:\n" + json.dumps(_values_only(ext), indent=1),
                system=REVIEW_SYSTEM.format(field_rules=_field_rules(), vocab=_VOCAB_TXT), tools=[REVIEW_TOOL],
                tool_choice={"type": "tool", "name": "record_review"},
                model=_resolve_model(REVIEW_MODEL) if REVIEW_MODEL else _main_model(model), max_tokens=max_tokens)

def _parse_review(r):
    if not isinstance(r, dict) or r.get("error"): raise RuntimeError(f"review failed: {r.get('error') if isinstance(r, dict) else r}")
    tu = r.get("tool_use"); tu = tu[0] if isinstance(tu, list) else tu
    if not tu: raise RuntimeError("review returned no tool call")
    return _unwrap(tu["input"] if "input" in tu else tu, ["verdicts", "revised_caveats", "missing_evidence", "paper_models"])

def review(tex, ext, model=None):
    return _parse_review(_llm_tool(_review_request(tex, ext, model)))

def review_many(tex, exts, model=None):
    """One review per extraction dict, all calls in parallel. -> list of review dicts or Exceptions."""
    out = []
    for r in _llm_tool([_review_request(tex, e, model) for e in exts], max_concurrency=8):
        try: out.append(_parse_review(r))
        except Exception as ex: out.append(ex)
    return out

def apply_review(ext, rev):
    import copy
    ext = copy.deepcopy(ext); log = []; drop = {}
    valid = {"fix_error_type": ("error_type", list(ERROR_TYPES)), "fix_frame": ("frame", ["observed", "rest", "n/a"])}
    for key in ("verdicts", "missing_evidence", "missing_observed_periods", "revised_caveats"):
        rev[key] = _coerce_list(rev.get(key, []), key)
    n = len(ext.get("models", []))
    idx = [x.get("model_index") for key in ("verdicts", "missing_observed_periods", "revised_caveats")
           for x in rev.get(key, []) if isinstance(x.get("model_index"), int)]
    one_based = bool(idx) and min(idx) >= 1 and max(idx) == n            # reviewer counted models from 1
    for key in ("verdicts", "missing_evidence", "missing_observed_periods", "revised_caveats"):
        for x in rev.get(key, []):
            mi = x.get("model_index", 0)
            mi = (mi - 1 if one_based else mi) if isinstance(mi, int) else 0
            x["model_index"] = mi if 0 <= mi < n else None
    for v in rev["verdicts"]:
        if v.get("action", "keep") == "keep": continue
        it = v.get("item", "")
        if v["model_index"] is None:
            log.append(dict(model=None, item=it, action=v.get("action"), reason=(v.get("reason") or "") + " [NOT APPLIED: invalid model index]", applied=False)); continue
        m = ext["models"][v["model_index"]]
        mm = re.match(r"(observed_periods|evidence)\[(\d+)\]", it)
        if mm and not (0 <= int(mm.group(2)) < len(m.get(mm.group(1)) or [])):
            log.append(dict(model=v["model_index"], item=it, action=v.get("action"), reason=(v.get("reason") or "") + " [NOT APPLIED: no such entry]", applied=False)); continue
        tgt = m[mm.group(1)][int(mm.group(2))] if mm else m.get(it)
        if not isinstance(tgt, dict): continue
        v.setdefault("reason", "")
        entry = dict(model=v["model_index"], item=it, action=v["action"], new_value=v.get("new_value"),
                     old_value=tgt.get("value", tgt.get("evidence_type")), reason=v["reason"], applied=True)
        if v["action"] == "clear":
            drop.setdefault(v["model_index"], []).append(it)
        elif v["action"] == "fix_cited_from":
            if (v.get("new_value") or "").strip(): tgt["cited_from"] = v["new_value"].strip()
            else: entry["applied"] = False
        elif v["action"] == "fix_evidence_note":
            if mm and mm.group(1) == "evidence" and (v.get("new_value") or "").strip(): tgt["evidence_note"] = v["new_value"].strip()
            else: entry["applied"] = False
        else:
            attr, opts = valid[v["action"]]
            nv = (v.get("new_value") or "").strip()
            if v["action"] == "fix_frame" and nv in ("observed", "rest"):
                word = r"observ\w*[- ]frame|observer|earth[- ]frame" if nv == "observed" else r"rest[- ]?frame"
                if not re.search(word, tgt.get("quote") or "", re.I):
                    entry["applied"] = False; entry["reason"] += " [NOT APPLIED: the value's quote does not state this frame]"
                    log.append(entry); continue
            if nv in opts: tgt[attr] = nv
            else: entry["applied"] = False           # invalid / empty fix -> ignored
        log.append(entry)
    for mi, items in drop.items():
        m = ext["models"][mi]
        for key in ("observed_periods", "evidence"):
            for i in sorted({int(re.search(r"\d+", it).group()) for it in items if it.startswith(key)}, reverse=True):
                if 0 <= i < len(m.get(key) or []): m[key].pop(i)
        for it in items:
            if it in QFIELDS: m[it] = {"value": None, "unit": "", "error_type": "Representative",
                                       "quote": "", "note": "cleared by reviewer (see review_log)"}
    for me in rev.get("missing_evidence", []):
        mi = me.get("model_index")
        if mi is None: continue
        e = {k: me[k] for k in _EVID["properties"] if k in me}
        ext["models"][mi].setdefault("evidence", [])
        have = {(x.get("evidence_type"), x.get("evidence_note")) for x in ext["models"][mi]["evidence"]}
        if (e.get("evidence_type"), e.get("evidence_note")) not in have:
            ext["models"][mi]["evidence"].append(e)
            log.append(dict(model=mi, item="evidence[+]", action="add_evidence", new_value=f"{e.get('evidence_type')} / {e.get('evidence_note')}",
                            old_value=None, reason="reviewer: omitted by extraction", applied=True))
    for mp in rev.get("missing_observed_periods", []):
        mi = mp.get("model_index")
        if mi is None: continue
        p = {k: mp[k] for k in _PER["properties"] if k in mp}
        ext["models"][mi].setdefault("observed_periods", [])
        have = {(x.get("value"), x.get("unit")) for x in ext["models"][mi]["observed_periods"]}
        if (p.get("value"), p.get("unit")) not in have and p.get("value") is not None:
            ext["models"][mi]["observed_periods"].append(p)
            log.append(dict(model=mi, item="observed_periods[+]", action="add_observed_period", new_value=f"{p.get('value')} {p.get('unit')}",
                            old_value=None, reason="reviewer: omitted by extraction", applied=True))
    for rc in rev.get("revised_caveats", []):
        if rc.get("model_index") is not None and (rc.get("caveats") or "").strip():
            ext["models"][rc["model_index"]]["caveats"] = rc["caveats"].strip()
    return ext, log

