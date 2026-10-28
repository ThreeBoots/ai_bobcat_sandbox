# ----------------------------------------------------------------- published versions (saved HTML / PDF)
_BLOCKS = ["p", "h1", "h2", "h3", "h4", "h5", "li", "figcaption", "caption", "tr", "table", "div", "section",
           "blockquote", "dt", "dd", "br"]
_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                         "september", "october", "november", "december"], 1)}

def _parse_date(s):
    """'23 June 2015', '2013 September 11', 'June 23 2015', '2015-06-23' -> 'YYYY-MM-DD' or None."""
    s = s.strip().replace(",", " ").replace(".", " ")
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m: return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    t = s.split()[:3]
    if len(t) < 3: return None
    mon = lambda w: next((v for k, v in _MONTHS.items() if w.isalpha() and k.startswith(w.lower()[:3])), None)
    if t[0].isdigit() and len(t[0]) == 4 and mon(t[1]) and t[2].isdigit(): y, mo, d = int(t[0]), mon(t[1]), int(t[2])   # 2013 September 11
    elif t[0].isdigit() and mon(t[1]) and t[2].isdigit(): y, mo, d = int(t[2]), mon(t[1]), int(t[0])                   # 23 June 2015
    elif mon(t[0]) and t[1].isdigit() and t[2].isdigit(): y, mo, d = int(t[2]), mon(t[0]), int(t[1])                   # June 23 2015
    else: return None
    return f"{y:04d}-{mo:02d}-{d:02d}" if 1 <= d <= 31 and 1800 < y < 2100 else None

def find_received_date(text):
    m = re.search(r"Received(?:\s+in\s+original\s+form)?\s*:?\s*((?:\d{1,2}\s+[A-Za-z]{3,9}\.?\s+\d{4})|(?:\d{4}\s+[A-Za-z]{3,9}\.?\s+\d{1,2})|(?:\d{4}-\d{2}-\d{2}))", text)
    return _parse_date(m.group(1).replace(".", "")) if m else None

def html_to_text(path):
    """Article text from a saved publisher page (IOP, OUP, Nature, A&A, Springer, ...).
    -> (text, meta) where meta holds citation_* metadata and the received date if found."""
    from bs4 import BeautifulSoup
    raw = open(path, encoding="utf-8", errors="replace").read()
    s = BeautifulSoup(raw, "lxml")
    meta = {}
    for tag in s.find_all("meta"):
        n = (tag.get("name") or tag.get("property") or "").lower()
        if n.startswith(("citation_", "dc.", "prism.")) and tag.get("content"):
            meta.setdefault(n, tag["content"])
    body = None
    for sel in ["div[itemprop='articleBody']", "div[data-widgetname='ArticleFulltext']", "div.article-body",
                "div.c-article-body", "div#article-body", "section.article-section", "article", "main", "body"]:
        body = s.select_one(sel)
        if body is not None and len(body.get_text(" ", strip=True)) > 3000: break
    abstract = None
    for sel in ["div.article-text", "section.abstract", "div.abstract", "section[class*=abstract]", "div[class*=abstract]"]:
        a = s.select_one(sel)
        if a is not None and (body is None or a not in body.parents) and 200 < len(a.get_text(" ", strip=True)) < 6000:
            abstract = a; break
    parts = []
    for root in [x for x in (abstract, body) if x is not None]:
        root = BeautifulSoup(str(root), "lxml")
        for sc in root.find_all("script"):
            t = (sc.get("type") or "").lower()
            if "math/tex" in t: sc.replace_with(f" ${sc.string or ''}$ ")
            else: sc.decompose()
        for t in root.find_all(["style", "button", "svg", "nav", "noscript", "mjx-math", "iframe", "form"]): t.decompose()
        for mt in root.find_all("math"):
            alt = mt.get("alttext") or ""
            if not alt:
                ann = mt.find("annotation", attrs={"encoding": re.compile("tex", re.I)})
                alt = ann.get_text() if ann else ""
            if alt: mt.replace_with(f" ${alt.strip()}$ ")
        seen_tables = set()
        for tb in root.find_all("table"):
            rows = [" | ".join(" ".join(c.get_text(" ", strip=True).split()) for c in tr.find_all(["th", "td"]))
                    for tr in tb.find_all("tr")]
            key = "\n".join(rows)
            if key in seen_tables: tb.decompose(); continue
            seen_tables.add(key); tb.replace_with("\n[TABLE]\n" + key + "\n[/TABLE]\n")
        for img in root.find_all("img"):
            img.replace_with(f" {img.get('alt')} " if img.get("alt") and len(img.get("alt")) < 80 else " ")
        for t in root.find_all(_BLOCKS):
            t.insert_before("\n"); t.insert_after("\n")
        txt = root.get_text("")
        lines = [" ".join(l.split()) for l in txt.split("\n")]
        parts.append("\n".join(l for l in lines if l))
    text = "\n\n".join(parts)
    text = re.split(r"\n(?:REFERENCES|References|Bibliography)\n", text)[0]          # drop reference list
    text = re.sub(r"\n(?:Download figure:?|Standard image|High-resolution image|Open in new tab|Download slide|Export to PPT)[^\n]*", "", text)
    text = re.sub(r"(\$[^$\n]{1,400}\$)(\s*\1)+", r"\1", text)          # IOP repeats each TeX equation
    text = re.sub(r"[\u2060\u200b\u200c\u200d\ufeff]", "", text)            # zero-width characters
    text = re.sub(r"\n{3,}", "\n\n", text)
    page = " ".join(s.get_text(" ").split())
    rec = find_received_date(page)
    title = meta.get("citation_title") or (s.title.get_text(strip=True) if s.title else "")
    head = f"TITLE: {title}\nJOURNAL: {meta.get('citation_journal_title', '')}  DOI: {meta.get('citation_doi', '')}\n" + \
           (f"RECEIVED: {rec}\n" if rec else "")
    meta.update(received_date=rec, title=title, doi=meta.get("citation_doi", "").replace("doi:", ""), format="html", path=path)
    return head + "\n" + text, meta

def pdf_meta_text(path):
    """(text, meta) for a PDF of the published version."""
    text = pdf_to_text(path)
    text = re.sub("[\x02\ufffe]\\s*", "", text)                      # pdfium line-break hyphen marker (out\x02burst -> outburst)
    text = re.sub("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff\ud800-\udfff]", "", text)
    first = text[:6000]
    doi = (re.search(r"\b(10\.\d{4,9}/[^\s\"<>]+[^\s\"<>.,;)])", first) or [None, ""])[1]
    meta = dict(doi=doi, title="", received_date=find_received_date(text[:20000]), format="pdf", path=path)
    return text, meta

def _bib_parts(bibcode):
    m = re.match(r"(\d{4})([A-Za-z&.]+?)\.*(\d+)\.*([A-Z]?\d+)", bibcode or "")
    return (m.group(1), m.group(3), m.group(4).lstrip("L")) if m else (None, None, None)

def find_paper_file(papers_dir, bibcode=None, doi=None, title=None):
    """Locate the saved published version (HTML preferred over PDF) in papers_dir.
    Match order: DOI, then journal volume + first page from a bibcode, then fuzzy title."""
    import glob as _g
    cands = sorted(_g.glob(os.path.join(papers_dir, "*.htm*"))) + sorted(_g.glob(os.path.join(papers_dir, "*.pdf")))
    year, vol, page = _bib_parts(bibcode)
    best, best_score = None, 0.0
    norm = lambda t: re.sub(r"[^a-z0-9]", "", (t or "").lower())
    for f in cands:
        try:
            if f.lower().endswith(".pdf"):
                head = pdf_to_text(f)[:6000]; m = {}
                fdoi = (re.search(r"\b(10\.\d{4,9}/[^\s\"<>]+[^\s\"<>.,;)])", head) or [None, ""])[1]
            else:
                raw = open(f, encoding="utf-8", errors="replace").read(200000)
                m = {k.lower(): v for k, v in re.findall(r'<meta[^>]+name="([^"]+)"[^>]+content="([^"]*)"', raw)}
                fdoi = m.get("citation_doi", m.get("dc.identifier", "")).replace("doi:", "")
                head = raw[:6000]
        except Exception:
            continue
        score = 0.0
        stem = os.path.splitext(os.path.basename(f))[0]
        if bibcode and stem.replace(" ", "") == bibcode.replace(" ", ""): score = 10
        elif doi and fdoi and fdoi.lower().strip() == doi.lower().strip(): score = 10
        elif vol and page and m.get("citation_volume") == vol and m.get("citation_firstpage", "").lstrip("L") == page: score = 9
        elif title:
            tt = re.search(r"<title[^>]*>(.*?)</title>", head if not f.lower().endswith(".pdf") else "", re.S | re.I)
            ft = m.get("citation_title") or (tt.group(1) if tt else "") or os.path.basename(f)
            score = 8 * difflib.SequenceMatcher(None, norm(title), norm(ft)).ratio()
        if f.lower().endswith(".pdf"): score -= 0.5          # prefer HTML when both exist
        if score > best_score: best, best_score = f, score
    return best if best_score >= 6 else None

def load_published(path):
    return pdf_meta_text(path) if path.lower().endswith(".pdf") else html_to_text(path)


VOCAB_FIX_TOOL = {"name": "record_fixes", "description": "Corrected evidence categories.",
    "input_schema": {"type": "object", "properties": {"fixes": {"type": "array", "items": {"type": "object", "properties": {
        "index": {"type": "integer"}, "evidence_type": {"type": "string"}, "evidence_note": {"type": "string"},
        "waveband": {"type": "string", "description": "One allowed waveband (for multi-band evidence pick the main one)."},
        "reason": {"type": "string"}}, "required": ["index", "evidence_type", "evidence_note", "reason"]}}},
        "required": ["fixes"]}}

def _unwrap(obj, required):
    """Return the dict that holds the expected top-level keys; models occasionally nest their tool output one level
    down under a stray key, or return it as a JSON string."""
    if isinstance(obj, str):
        try: obj = _loads_tolerant(obj.replace("\\'", "'"))
        except ValueError: return {}
    if not isinstance(obj, dict): return {}
    if any(k in obj and obj[k] not in (None, [], "") for k in required): return obj
    for v in obj.values():
        if isinstance(v, (dict, str)):
            u = _unwrap(v, required)
            if u: return u
    return obj

def _fix_escapes(t):
    """Double every backslash that does not start a valid JSON escape (LaTeX such as \\sim inside quotes)."""
    return re.sub(r"\\(.)", lambda m: m.group(0) if m.group(1) in '"\\/bfnrtu' else "\\\\" + m.group(1), t, flags=re.S)

def _loads_tolerant(s):
    """json.loads that also accepts raw control characters (e.g. line breaks copied from PDF text into quotes) and
    invalid backslash escapes (LaTeX inside quotes) in string-encoded tool output. Raises ValueError if hopeless."""
    for f in (lambda t: json.loads(t), lambda t: json.loads(t, strict=False),
              lambda t: json.loads(_fix_escapes(t), strict=False)):
        try: return f(s)
        except ValueError: pass
    raise ValueError("undecodable JSON string")

def _coerce_list(x, key):
    """Tool outputs occasionally arrive as a JSON string (sometimes wrapping {key: [...]}, sometimes with \\'
    escapes). Return a list of dicts."""
    if isinstance(x, str):
        s = x.strip().replace("\\'", "'")
        try: x = _loads_tolerant(s)
        except ValueError: return []
    if isinstance(x, dict): x = x.get(key, [x])
    out = []
    for i in (x or []):
        if isinstance(i, str):
            try: i = _loads_tolerant(i)
            except ValueError: continue
        if isinstance(i, dict): out.append(i)
    return out

def fix_evidence(ext, tpl, model=None):
    """Repair evidence entries whose type/note pair is not in the Sheet2 vocabulary (one small LLM call), then drop
    exact duplicates (same type, note and waveband). Returns a log list."""
    log = []
    allowed = "\n".join(f"  {t}: {'; '.join(n)}" for t, n in tpl["vocab"].items())
    for mi, m in enumerate(ext["models"]):
        ev = m.get("evidence", [])
        wbl = {w.lower(): w for w in tpl["wavebands"]}
        for e in ev:                                        # case-only waveband fixes need no model call
            w = (e.get("waveband") or "").strip()
            if w.lower() in wbl: e["waveband"] = wbl[w.lower()]
        split = []                                          # several wavebands in one entry -> one entry per band
        for i, e in enumerate(ev):
            w = (e.get("waveband") or "").strip()
            if not w or w in tpl["wavebands"]: split.append(e); continue
            bands = [b.strip() for b in str(_norm_waveband(w, tpl["wavebands"])).split(";") if b.strip() in tpl["wavebands"]]
            if len(bands) >= 2:
                for b in bands: split.append(dict(e, waveband=b))
                log.append(dict(model=mi, item=f"evidence[{i}]", action="split_waveband", old_value=w, new_value="; ".join(bands),
                                reason="one entry per waveband (downstream database allows one waveband per entry)", applied=True))
            else:
                if len(bands) == 1: e["waveband"] = bands[0]
                split.append(e)
        ev = m["evidence"] = split
        bad = [(i, e) for i, e in enumerate(ev) if e.get("evidence_note") not in tpl["vocab"].get(e.get("evidence_type"), [])
               or e.get("waveband") not in tpl["wavebands"]]
        if bad:
            items = "\n".join(f"[{i}] type={e.get('evidence_type')!r} note={e.get('evidence_note')!r} waveband={e.get('waveband')!r} quote={e.get('quote','')[:250]!r}" for i, e in bad)
            try:
                r = _llm_tool(dict(prompt=f"Allowed evidence types and notes:\n{allowed}\nAllowed wavebands: {', '.join(tpl['wavebands'])}\n\n"
                                    f"These entries use a type/note pair or waveband that is not allowed. For each, choose the allowed "
                                    f"type, note and waveband that best describe the quoted evidence.\n{items}",
                             tools=[VOCAB_FIX_TOOL], tool_choice={"type": "tool", "name": "record_fixes"},
                             model=_main_model(model), max_tokens=1500))
                tu = r.get("tool_use"); tu = tu[0] if isinstance(tu, list) else tu
                fixes = _coerce_list(_unwrap(tu["input"] if "input" in tu else tu, ["fixes"]).get("fixes", []), "fixes")
                for fx in fixes:
                    i = fx["index"]
                    if 0 <= i < len(ev) and fx.get("waveband") in tpl["wavebands"]: ev[i]["waveband"] = fx["waveband"]
                    if 0 <= i < len(ev) and fx["evidence_note"] in tpl["vocab"].get(fx["evidence_type"], []):
                        log.append(dict(model=mi, item=f"evidence[{i}]", action="vocab_fix",
                                        old_value=f"{ev[i].get('evidence_type')} / {ev[i].get('evidence_note')}",
                                        new_value=f"{fx['evidence_type']} / {fx['evidence_note']}", reason=fx["reason"], applied=True))
                        ev[i]["evidence_type"], ev[i]["evidence_note"] = fx["evidence_type"], fx["evidence_note"]
            except Exception as ex:
                log.append(dict(model=mi, item="evidence", action="vocab_fix", reason=f"failed: {ex}", applied=False))
        wb = {w.lower(): w for w in tpl["wavebands"]}
        for i, e in enumerate(ev):
            w = (e.get("waveband") or "").strip()
            if w and w not in tpl["wavebands"] and w.lower() in wb:
                e["waveband"] = wb[w.lower()]
            q = e.get("quote") or ""
            if e.get("evidence_note") == "Discrete bursts with periodicity" and \
               re.search(r"\bdips?\b|drop-?outs?\b|\bdeclines?\b|\bdecreases?\b", q, re.I) and \
               not re.search(r"burst|flare|outburst", q, re.I):
                log.append(dict(model=mi, item=f"evidence[{i}]", action="burst_rule", old_value="continuum_variability / Discrete bursts with periodicity",
                                new_value="continuum_variability / Other continuum flux variation",
                                reason="quote describes a dip/decline, not two or more recurring bursts", applied=True))
                e["evidence_type"], e["evidence_note"] = "continuum_variability", "Other continuum flux variation"
        seen, keep = set(), []
        for i, e in enumerate(ev):
            key = (e.get("evidence_type"), e.get("evidence_note"), e.get("waveband"))
            if key in seen:
                log.append(dict(model=mi, item=f"evidence[{i}]", action="drop_duplicate", old_value=" / ".join(map(str, key)),
                                reason="same type, note and waveband as an earlier entry", applied=True))
                continue
            seen.add(key); keep.append(e)
        m["evidence"] = keep
    return log

def _resolve_ids(paper_id):
    """Classify paper_id -> dict(bib, aid, doi, path)."""
    pid = paper_id.strip()
    if os.path.isfile(pid): return dict(bib=None, aid=None, doi=None, path=pid)
    if re.match(r"^10\.\d{4,9}/", pid): return dict(bib=None, aid=None, doi=pid, path=None)
    if re.match(r"^\d{4}\.\d{4,5}(v\d+)?$|^[a-z\-]+/\d{7}$", pid): return dict(bib=None, aid=pid, doi=None, path=None)
    m = re.match(r"^\d{4}arXiv(\d{4})(\d{4,5})[A-Z.]$", pid)
    if m: return dict(bib=pid, aid=f"{m.group(1)}.{m.group(2)}", doi=None, path=None)
    return dict(bib=pid, aid=None, doi=None, path=None)

MIN_TEXT_CHARS = 2000   # below this the paper text is unusable (e.g. image-only ADS scans)
_ARXIV_EXEC = [None]

def _arxiv_pool():
    if _ARXIV_EXEC[0] is None:
        _ARXIV_EXEC[0] = concurrent.futures.ThreadPoolExecutor(4)
    return _ARXIV_EXEC[0]

def _arxiv_lookup(bib, aid):
    """bibcode/arXiv id -> (arxiv_id, title, journal_ref, arxiv_meta dict); never raises (errors go in the dict)."""
    title, jr, am = "", "", {}
    try:
        if bib and not aid and "arXiv" not in bib: aid, title, jr = bibcode_to_arxiv(bib)
        if aid: am = arxiv_meta(aid); title = title or am["title"]; jr = jr or am["journal_ref"]
    except Exception as ex:
        am = {"error": str(ex)}
    return aid, title, jr, am

def _attach_source_log(ext, entries):
    """Paper-level evidence-checker log entries: shown on every model's review sheet (model=None)."""
    return [dict(e) for e in entries]

def recover_missing_models(tex, tpl, ext2, rev, log, model=None):
    """If the review's paper_models list contains models the extraction missed, extract them in targeted passes
    (RECOVERY_BATCH models per call, in parallel; no limit on the number), review the new models in the same batches,
    and append them to ext2. -> (ext2, log)."""
    wanted = missing_models(rev, len(ext2.get("models", [])))
    if not wanted: return ext2, log
    names = "; ".join(f"{w.get('source_nickname')} / {w.get('model_label')}" for w in wanted)
    new, errs = extract_missing_models(tex, tpl, ext2, wanted, model)
    for e in errs: log.append(dict(model=None, item="models[+]", action="add_missing_model", reason=e, applied=False))
    if not new:
        if not errs:
            log.append(dict(model=None, item="models[+]", action="add_missing_model", new_value=names,
                            reason="targeted extraction returned no model (judged not a distinct model)", applied=False))
        return ext2, log
    base = {k: v for k, v in ext2.items() if k != "models"}
    subs = [dict(base, models=new[i:i + RECOVERY_BATCH]) for i in range(0, len(new), RECOVERY_BATCH)]
    revs = review_many(tex, subs, model)
    off = len(ext2.get("models", [])); added, log2 = [], []
    for sub, rv in zip(subs, revs):
        if isinstance(rv, Exception):
            sub2, lg = sub, [dict(model=None, item="review", action="review_added_models", reason=f"failed: {rv}", applied=False)]
        else:
            sub2, lg = apply_review(sub, rv)
        for l in lg:
            if isinstance(l.get("model"), int): l["model"] += off + len(added)
        log2 += lg; added += sub2["models"]
    for i, m in enumerate(added):
        m["extraction_notes"] = ((m.get("extraction_notes") or "") +
                                 " [Model added by a targeted second extraction: the review listed it as missing]").strip()
        log.append(dict(model=off + i, item="models[+]", action="add_missing_model",
                        new_value=f"{m.get('source_nickname')} / {m.get('model_label')}",
                        reason="reviewer paper_models: not covered by the first extraction", applied=True))
    ext2["models"] = ext2.get("models", []) + added
    return ext2, log + log2

REQUIRE_EVIDENCE = True   # user, Oct 27: every output model needs >= 1 evidence entry; models without evidence are dropped

def drop_models_without_evidence(ext, log):
    """User rule (Oct 27): a model with parameters but no evidence entry (after the false-positive check and fix_evidence)
    is not written. Dropped models are kept in ext['models_dropped_no_evidence'] (raw JSON) and logged as a paper-level
    review_log entry (action drop_model_no_evidence) so every workbook shows them; log entries of the remaining models are
    re-indexed. -> list of new log entries."""
    if not REQUIRE_EVIDENCE: return []
    ms = ext.get("models") or []
    keep = [i for i, m in enumerate(ms) if m.get("evidence")]
    if len(keep) == len(ms): return []
    remap = {old: new for new, old in enumerate(keep)}
    for l in log:
        if isinstance(l, dict) and isinstance(l.get("model"), int):
            l["model"] = remap.get(l["model"], f"dropped model {l['model'] + 1}")
    dropped = [dict(model_index=i + 1, model_label=m.get("model_label"), source_nickname=m.get("source_nickname") or ext.get("source_nickname"),
                    filled=[k for k in QFIELDS if isinstance(m.get(k), dict) and (m[k].get("value") is not None or m[k].get("range_low") is not None)])
               for i, m in enumerate(ms) if i not in remap]
    ext["models_dropped_no_evidence"] = (ext.get("models_dropped_no_evidence") or []) + [dict(d, model=ms[d["model_index"] - 1]) for d in dropped]
    ext["models"] = [ms[i] for i in keep]
    return [dict(model=None, item=f"model {d['model_index']} ({d['source_nickname']})", action="drop_model_no_evidence",
                 old_value=str(d["model_label"]), new_value="not written",
                 reason="no evidence entry for this model after the evidence checks; parameters filled: " + (", ".join(d["filled"]) or "none"),
                 applied=True) for d in dropped]

def run(paper_id, template_xlsx, out_dir=".", model=None, papers_dir=None, text=None, text_source=None, evidence_model=None):
    """Extract one paper into a BOBcat workbook.
    paper_id: ADS bibcode, DOI, arXiv id, or a path to a saved published HTML/PDF.
    papers_dir: folder of saved PUBLISHED versions (HTML 'Save Page As' or PDF); matched by DOI, bibcode
      volume+first page, or title. The published text is used whenever found (guidelines: published > preprint);
      otherwise the latest arXiv LaTeX is used and flagged. text=: explicit published text (overrides both).
    Returns (xlsx_paths, ext_raw, review, ext_final); xlsx_paths has one workbook per model."""
    os.makedirs(out_dir, exist_ok=True)
    tpl = load_template(template_xlsx)
    ids = _resolve_ids(paper_id); bib, aid, doi = ids["bib"], ids["aid"], ids["doi"]
    title, jr, am, pub_meta = "", "", {}, {}
    # arXiv metadata (optional: not every paper has a preprint). Looked up in the BACKGROUND while the model calls run:
    # it is only needed for a title-based file match, the arXiv LaTeX fallback, the title check and the epoch fallback.
    fut = _arxiv_pool().submit(_arxiv_lookup, bib, aid) if (aid or (bib and "arXiv" not in bib)) else None
    joined = [fut is None]
    def _join():
        nonlocal aid, title, jr, am
        if not joined[0]:
            a2, t2, j2, am = fut.result(); aid = aid or a2; title = title or t2; jr = jr or j2; joined[0] = True
    # published version: DOI / bibcode match first (no arXiv needed); the arXiv title only if that fails
    pub_path = ids["path"]
    if not pub_path and papers_dir:
        pub_path = find_paper_file(papers_dir, bibcode=bib, doi=doi)
        if not pub_path and fut is not None:
            _join(); pub_path = find_paper_file(papers_dir, bibcode=bib, doi=doi, title=title) if title else None
    if not text and not pub_path: _join()          # arXiv LaTeX fallback needs the arXiv id
    if text:
        tex, tsrc = text, text_source or "user-supplied text (published version)"
    elif pub_path:
        tex, pub_meta = load_published(pub_path)
        title = pub_meta.get("title") or title; doi = doi or pub_meta.get("doi")
        tsrc = f"published version ({pub_meta.get('format')}): {os.path.basename(pub_path)}"
    elif aid:
        tex = fetch_arxiv_tex(aid)
        tsrc = (f"arXiv {aid}{am.get('version', '')} LaTeX (no published version found"
                f"{' in ' + papers_dir if papers_dir else ''}). Guidelines require the PUBLISHED version: verify values.")
    else:
        raise LookupError(f"No published file found for {paper_id!r} and no arXiv version; add it to papers_dir.")
    tex = (tex or "").replace("\r\n", "\n").replace("\r", "\n")   # PDF text: CR line ends end up inside verbatim quotes
    ntext = len(re.sub(r"\s+", " ", tex or "").strip())
    if ntext < MIN_TEXT_CHARS:
        raise ValueError(f"Only {ntext} characters of text in {tsrc!r}: probably an image-only scan with no text layer. "
                         "OCR it (or save a version with a text layer) before extracting.")
    _tm = {"t0": time.time()}
    ext, raw = extract(tex, tpl, model, evidence_model=evidence_model)
    _tm["extract"] = time.time()
    _no_rev = {"verdicts": [], "revised_caveats": [], "paper_models": []}
    if EVIDENCE_CHECKERS:          # [omission check +] value review run together; then union -> false-positive check
        rv, ev_check_log = check_evidence(tex, tpl, ext, review_req=_review_request(tex, ext, model) if VALUE_REVIEW else None)
        rev = _parse_review(rv) if VALUE_REVIEW else _no_rev
    else:
        rev, ev_check_log = (review(tex, ext, model) if VALUE_REVIEW else _no_rev), []
    _join()                                        # arXiv lookup has finished in the background by now
    link = (f"https://ui.adsabs.harvard.edu/abs/{bib}/abstract" if bib else
            f"https://doi.org/{doi}" if doi else f"https://arxiv.org/abs/{aid}")
    ext2, log = apply_review(ext, rev)
    _tm["review_and_evidence_checks"] = time.time()
    ext2, log = recover_missing_models(tex, tpl, ext2, rev, log, model)
    _tm["recover_models"] = time.time()
    assign_evidence(ext2)
    log += evidence_dropped_log(ext2)
    log += _attach_source_log(ext2, ev_check_log)
    if ext2.get("evidence_error"): log.append(dict(model=None, item="evidence", action="evidence_step", reason=ext2["evidence_error"], applied=False))
    log += fix_evidence(ext2, tpl, model)
    log += drop_models_without_evidence(ext2, log)
    _tm["fix_evidence"] = time.time()
    import difflib as _dl
    _norm = lambda x: re.sub(r"[^a-z0-9 ]", "", (x or "").lower())
    ptitle = ext2.get("paper_title") or ""
    if am.get("title") and ptitle and _dl.SequenceMatcher(None, _norm(am["title"]), _norm(ptitle)).ratio() < 0.6:
        log.append(dict(action="arxiv_match_rejected", arxiv_id=aid, arxiv_title=am["title"], paper_title=ptitle))
        if not pub_meta.get("title"): title = ""
        aid, am = None, {"error": f"arXiv match rejected: title differs from the paper ({am['title']!r})"}
    if pub_path and not pub_meta.get("title") and ptitle: title = ptitle   # PDF: use the title printed in the paper
    title = title or ptitle or ""
    rec = pub_meta.get("received_date") or ext2.get("journal_received_date")   # page metadata beats the LLM
    if pub_meta.get("received_date"): ext2["journal_received_date"] = rec
    try:
        mjd = date_to_mjd(rec); y, mo, dd = (int(x) for x in str(rec)[:10].split("-"))
        ext2["_fallback_epoch"] = dict(mjd=mjd, formula=f"=DATE({y},{mo},{dd})-DATE(1858,11,17)",
                                       note=f"no epoch in paper; journal received date {rec} -> MJD")
    except (TypeError, ValueError):
        arxiv_only = bool(bib and "arXiv" in bib) and not am.get("journal_ref")
        if am.get("published") and arxiv_only:
            y, mo, dd = (int(x) for x in am["published"].split("-"))
            ext2["_fallback_epoch"] = dict(mjd=date_to_mjd(am["published"]), formula=f"=DATE({y},{mo},{dd})-DATE(1858,11,17)",
                note=f"no epoch in paper; arXiv-only paper, submission date {am['published']} -> MJD")
        elif am.get("published"):
            y, mo, dd = (int(x) for x in am["published"].split("-"))
            ext2["_fallback_epoch"] = dict(mjd=date_to_mjd(am["published"]), formula=f"=DATE({y},{mo},{dd})-DATE(1858,11,17)",
                note=f"no epoch in paper and no journal received date in source; arXiv v1 date {am['published']} used as proxy",
                probs=["EPOCH PROXY: arXiv v1 date used because the journal submission date is not in the source - replace with the journal received date"])
    sheets = build_rows(ext2, tpl, tex, link, model)
    _tm["build_rows"] = time.time()
    _keys = list(_tm); ext2["_stage_seconds"] = {k: round(_tm[k] - _tm[p_], 1) for p_, k in zip(_keys, _keys[1:])}
    tag = paper_filename(title or paper_id)
    role_llm = ext2.get("paper_role", "")
    role, role_reason, role_log = definition_role(role_llm, ext2.get("paper_role_reason", ""), sheets)
    if role_log: log.append(role_log); ext2["paper_role"], ext2["paper_role_reason"] = role, role_reason
    status = "AUTOMATED DRAFT - requires human verification before ingestion"
    if ext2.get("value_extraction_failed"):
        status += ". WARNING: VALUE EXTRACTION FAILED (model output carried no parameter values after re-asking): re-run this paper"
    if not sheets:
        status += ". WARNING: NO MODEL WRITTEN - no model had an evidence entry (see models_dropped_no_evidence in the raw JSON)"
    if role == "non_binary_alternative_only":
        status += ". WARNING: paper classified as proposing only a non-binary origin - per guidelines NOT a BOBcat entry"
    meta = dict(bibcode=bib, doi=doi, arxiv_id=aid, title=title, journal_ref=jr or pub_meta.get("citation_journal_title", ""),
                paper_link=link, text_source=tsrc, journal_received_date=rec,
                paper_role=role, paper_role_reason=ext2.get("paper_role_reason", ""), paper_role_llm=role_llm,
                paper_models_review=[f"{pm.get('source_nickname')} | {pm.get('model_label')} -> extraction model "
                                     f"{pm.get('extracted_model_index')}" for pm in _coerce_list(rev.get('paper_models', []), 'paper_models')
                                     if isinstance(pm, dict)],
                pta_targeted_limit=ext2.get("pta_targeted_limit"),
                pta_limit_action=("add this paper/source as a 'PTA limit' row in the master list" if ext2.get("pta_targeted_limit") else ""),
                extraction_model=raw.get("model"), evidence_model=(ext.get("_models_used") or {}).get("evidence"), review_model=((_resolve_model(REVIEW_MODEL) if REVIEW_MODEL else raw.get("model")) if VALUE_REVIEW else "none (value review disabled)"),
                checker_models=str(ext.get("_checker_models") or ""), redshift_used=ext2.get("redshift"),
                redshift_quote=ext2.get("redshift_quote", ""), n_models=len(sheets),
                conversions="model sheets hold numbers; conversion formulas are listed on the provenance sheets", status=status)
    paths = write_model_workbooks(out_dir, tag, sheets, tpl, meta, log)
    json.dump(dict(extraction=ext, review=rev, final=ext2, text_source=tsrc, meta=meta, log=log, text_path=pub_path or aid,
                   workbooks=[os.path.basename(p) for p in paths]),
              open(f"{out_dir}/{tag} (raw).json", "w"), indent=1, default=str)
    return paths, ext, rev, ext2

def _out_slug(paper_id):
    s = os.path.splitext(os.path.basename(paper_id))[0] if os.path.sep in paper_id else paper_id
    return re.sub(r"[^A-Za-z0-9.+_-]+", "_", s)[:80]

LAST_BATCH_CALLS = []   # per-request records (paper, task, model, tokens) of the last run_many

def run_many(paper_ids, template_xlsx, out_root=".", max_concurrency=16, window=0.5, **run_kw):
    """Run several papers at once. Each paper runs run() in its own thread; all their model calls are queued and sent
    together as list-form host.llm calls (up to max_concurrency at a time, host cap 32), so papers no longer wait for
    each other. Outputs go to <out_root>/<paper slug>/. Returns (results, batch_stats): results is one dict per paper
    (paper_id, out_dir, paths, seconds, error); batch_stats is one dict per model batch (n requests, seconds, errors,
    tokens) for spotting rate limits or slowdowns."""
    global _LLM_BATCHER
    b = LLMBatcher(max_concurrency=max_concurrency, window=window); _LLM_BATCHER = b
    def one(pid):
        threading.current_thread().name = pid
        t0 = time.time(); od = os.path.join(out_root, _out_slug(pid)); os.makedirs(od, exist_ok=True)
        try:
            paths = run(pid, template_xlsx, out_dir=od, **run_kw)[0]
            return dict(paper_id=pid, out_dir=od, paths=paths, seconds=round(time.time() - t0, 1), error=None)
        except Exception as ex:
            import traceback
            return dict(paper_id=pid, out_dir=od, paths=[], seconds=round(time.time() - t0, 1),
                        error=f"{type(ex).__name__}: {ex}", traceback=traceback.format_exc()[-1500:])
    try:
        with concurrent.futures.ThreadPoolExecutor(max(1, len(paper_ids))) as ex:
            results = list(ex.map(one, paper_ids))
    finally:
        _LLM_BATCHER = None; b.close()
    t0 = min([s["start"] for s in b.stats] or [0])
    stats = [dict(s, start=round(s["start"] - t0, 1)) for s in b.stats]
    global LAST_BATCH_CALLS
    LAST_BATCH_CALLS = list(b.calls)
    return results, stats

def rebuild_from_raw(raw_json, template_xlsx, text, out_dir=".", meta=None, link=None):
    """Regenerate a workbook from a saved '(raw).json' after code/rule fixes, without new extraction/review calls
    (fix_evidence may make one small vocabulary call). text: the paper text used originally."""
    tpl = load_template(template_xlsx); R = json.load(open(raw_json))
    fin = R["final"]; log = list(R.get("log") or []) + fix_evidence(fin, tpl)
    log += drop_models_without_evidence(fin, log)
    meta = dict(R.get("meta") or {}, **(meta or {}))
    sheets = build_rows(fin, tpl, text, link or meta.get("paper_link", ""))
    role_llm = meta.get("paper_role_llm") or fin.get("paper_role", "")
    role, reason, role_log = definition_role(role_llm, fin.get("paper_role_reason", ""), sheets)
    if role_log:
        log.append(role_log); fin["paper_role"], fin["paper_role_reason"] = role, reason
        meta.update(paper_role=role, paper_role_reason=reason, paper_role_llm=role_llm)
        if "non-binary" not in str(meta.get("status", "")):
            meta["status"] = str(meta.get("status", "")) + ". WARNING: paper classified as proposing only a non-binary origin - per guidelines NOT a BOBcat entry"
    if not sheets and "NO MODEL WRITTEN" not in str(meta.get("status", "")):
        meta["status"] = str(meta.get("status", "")) + ". WARNING: NO MODEL WRITTEN - no model had an evidence entry (see models_dropped_no_evidence in the raw JSON)"
    meta["n_models"] = len(sheets)
    tag = paper_filename(meta.get("title") or "rebuilt")
    paths = write_model_workbooks(out_dir, tag, sheets, tpl, meta, log)
    R.update(final=fin, log=log, meta=meta, workbooks=[os.path.basename(p) for p in paths])
    json.dump(R, open(os.path.join(out_dir, f"{tag} (raw).json"), "w"), indent=1, default=str)
    return paths, sheets

# ----------------------------------------------------------------- evaluation vs a hand extraction
def compare_to_gold(auto_df, gold_xlsx, rtol=0.01):
    """Field-by-field comparison of an automated model sheet with a hand-filled BOBcat sheet.
    Evidence is compared as sets of (type, note, waveband) regardless of slot order."""
    g = pd.read_excel(gold_xlsx, sheet_name=0)
    g = g[g["Name"].notna()].copy(); g["Name"] = g["Name"].astype(str).str.strip()
    a = auto_df.copy(); a["Name"] = a["Name"].astype(str).str.strip()
    blank = lambda x: x is None or (isinstance(x, float) and math.isnan(x)) or str(x).strip() in ("", "--", "nan")
    def ev(df):
        s = df.set_index("Name")["Value"].to_dict(); out = set()
        for k in range(1, 20):
            sfx = "" if k == 1 else str(k)
            t = s.get(f"evidence type{sfx}")
            if not blank(t): out.add((str(t), str(s.get(f"evidence type note{sfx}")), str(s.get(f"evidence type waveband{sfx}"))))
        return out
    rows = []
    skip = ("evidence type", "Summary", "Caveats", "Extension", "paper link")
    ga, aa = g.set_index("Name"), a.set_index("Name")
    for name in ga.index:
        if name.startswith(skip) or name not in aa.index: continue
        gv, av = ga.at[name, "Value"], aa.at[name, "Value"]
        ge, ae = ga.at[name, "Error type"], aa.at[name, "Error type"]
        if blank(gv) and blank(av): continue
        tol = (lambda x: 30.0) if "epoch" in name else (lambda x: rtol * max(abs(x), 1e-30))
        try: vmatch = abs(float(gv) - float(av)) <= tol(float(gv))
        except (TypeError, ValueError): vmatch = str(gv).strip().lower() == str(av).strip().lower()
        if blank(gv) != blank(av): vmatch = False
        tmatch = (blank(ge) and blank(ae)) or str(ge).strip().lower() == str(ae).strip().lower()
        rows.append(dict(Name=name, gold_value=gv, auto_value=av, gold_error=ga.at[name, "Error"], auto_error=aa.at[name, "Error"],
                         gold_error_type=ge, auto_error_type=ae, value_match=vmatch, error_type_match=tmatch))
    ge_, ae_ = ev(g), ev(a)
    for t in sorted(ge_ | ae_):
        rows.append(dict(Name="evidence: " + " / ".join(t), gold_value=t in ge_, auto_value=t in ae_,
                         value_match=(t in ge_) == (t in ae_), error_type_match=True))
    return pd.DataFrame(rows)
