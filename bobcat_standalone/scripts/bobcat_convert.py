# ----------------------------------------------------------------- conversions
MAX_TEXT = 750

def shorten_text(s, limit=MAX_TEXT, model=None):
    """Ask a small model to condense an over-length summary/caveat; returns the text (possibly unchanged)."""
    try:
        r = _host_llm(prompt=f"Condense the following to fewer than {limit - 40} characters. Keep every factual point and the "
                            f"same register; no preamble, output only the condensed text.\n\n{s}", max_tokens=600, model=model)
        t = (r.get("text") or "").strip()
        return t if 0 < len(t) < len(s) else s
    except Exception:
        return s

def clip_text(s, limit=MAX_TEXT, model=None):
    """Enforce the BOBcat character limit: condense with a small model, cut at a sentence end only as a last resort."""
    s = (s or "").strip()
    if len(s) < limit: return s, []
    t = shorten_text(s, limit, model)
    if len(t) < limit: return t, [f"condensed from {len(s)} to {len(t)} characters by model - check wording"]
    s = t
    cut = s[:limit - 1]; k = max(cut.rfind(". "), cut.rfind(".\n"))
    out = cut[:k + 1] if k > limit // 3 else cut.rstrip() + "\u2026"
    return out, [f"TRUNCATED from {len(s)} to {len(out)} characters - check wording"]

def date_to_mjd(iso):
    import datetime as dt
    d = dt.date.fromisoformat(str(iso)[:10])
    return float((d - dt.date(1858, 11, 17)).days)

def _norm(s):
    s = re.sub(r"\\[a-zA-Z]+|[{}$\\~^_]", "", s or "")
    return re.sub(r"[^a-z0-9.]", "", s.lower())

def verify_quote(q, tex):
    if not q: return "no quote"
    parts = [p for p in re.split(r"\s*(?:\.\.\.|\u2026|\[\.\.\.\])\s*", q) if len(_norm(p)) >= 8]
    if len(parts) > 1:
        res = [verify_quote(p, tex) for p in parts]
        bad = [r for r in res if r.startswith("QUOTE NOT FOUND")]
        return f"quote verified ({len(parts)} fragments)" if not bad else f"QUOTE NOT FOUND ({len(bad)}/{len(parts)} fragments)"
    nq, nt = _norm(q), _norm(tex)
    if nq and nq in nt: return "quote verified"
    sm = difflib.SequenceMatcher(None, nt, nq, autojunk=False)
    m = sm.find_longest_match(0, len(nt), 0, len(nq))
    frac = m.size / max(len(nq), 1)
    return "quote verified (fuzzy)" if frac > 0.8 else f"QUOTE NOT FOUND ({frac:.0%} match)"

def _lit(x):
    """Number literal for spreadsheet formulas."""
    return format(float(x), ".10g").replace("e", "E")

def _scalar_fn(kind, d, z):
    """Monotonic map from the stated unit to the sheet unit.
    -> (f numeric, desc, ndigits, fx formula-builder or None if no conversion, fe linear error-builder or None)."""
    u = (d.get("unit") or "").lower().strip(); fr = d.get("frame", "n/a")
    if kind == "log_mass":
        if "log" in u: return (lambda x: x), "as stated (log Msun)", 3, None, None
        mm = re.match(r"^\s*(?:x|×|\*)?\s*(?:1e([+-]?\d+)|10\s*\^\s*\{?([+-]?\d+)\}?|10\*\*([+-]?\d+))", u)
        if mm:
            p10 = int(next(g for g in mm.groups() if g is not None)); fac = 10.0 ** p10
            return (lambda x: math.log10(x * fac)), f"log10 of stated value x 1e{p10} Msun", 3, (lambda s: f"LOG10({s}*1E{p10})"), None
        return (lambda x: math.log10(x)), "log10 of stated Msun", 3, (lambda s: f"LOG10({s})"), None
    if kind == "days_earth":
        uy = re.sub(r"s$", "", u.replace("year", "yr")) if u not in ("d", "days", "day") else "d"
        yrs = {"yr": 1, "kyr": 1e3, "myr": 1e6, "gyr": 1e9, "month": 1 / 12, "mo": 1 / 12}.get(uy)
        hrs = {"hr": 1 / 24, "h": 1 / 24, "hour": 1 / 24, "week": 7, "wk": 7, "d": 1}.get(uy)
        if yrs is None and hrs is None: return None, f"unhandled unit '{u}'", None, None, None
        f = yrs * DAY_PER_YR if yrs is not None else hrs
        rest = fr == "rest" and z is not None
        k = f * ((1 + z) if rest else 1)
        parts = ([f"(1+{_lit(z)})"] if rest else []) + ([_lit(DAY_PER_YR)] if yrs is not None else []) + \
                ([_lit(yrs)] if yrs not in (None, 1) else []) + ([_lit(hrs)] if hrs not in (None, 1) else [])
        desc = f"{u} ({fr} frame)" + (f" x (1+z={1+z:g})" if rest else "") + (f" x {DAY_PER_YR} d/yr" if yrs is not None else "") + \
               (f" x {yrs:g}" if yrs not in (None, 1) else "") + (f" x {hrs:g} d/{uy}" if hrs not in (None, 1) else "") + \
               ("  WARNING: rest frame but z unknown" if fr == "rest" and z is None else "")
        fx = (lambda s: "*".join([s] + parts)) if parts else None
        return (lambda x: x * k), desc, 1, fx, fx
    if kind == "mjd":
        vals = [x for x in (d.get("value"), d.get("range_low"), d.get("range_high")) if isinstance(x, (int, float))]
        if u in ("mjd", "") and vals and max(vals) < 3000: u = "decimal_year"      # years mislabelled as MJD
        elif u in ("mjd", "") and vals and min(vals) > 2.4e6: u = "jd"
        if u in ("mjd", ""): return (lambda x: x), "as stated (MJD)", 0, None, None
        if u == "jd": return (lambda x: x - 2400000.5), "JD -> MJD", 0, (lambda s: f"{s}-2400000.5"), (lambda s: s)
        if u in ("decimal_year", "yr", "year"):
            return (lambda x: 51544.5 + (x - 2000.0) * DAY_PER_YR), "decimal year -> MJD", 0, \
                   (lambda s: f"51544.5+({s}-2000)*{_lit(DAY_PER_YR)}"), (lambda s: f"{s}*{_lit(DAY_PER_YR)}")
        return None, f"unhandled epoch unit '{u}'", None, None, None
    if kind == "arcsec":                         # angular separation -> arcsec
        uu = (u.replace("\u2033", "arcsec").replace('"', "arcsec").replace("\u03bc", "u").replace("\u00b5", "u")
              .replace("seconds of arc", "arcsec").replace("arcseconds", "arcsec").replace(" ", ""))
        f = {"arcsec": 1, "as": 1, "mas": 1e-3, "milliarcsec": 1e-3, "uas": 1e-6, "microarcsec": 1e-6}.get(uu)
        if f is None: return None, f"unhandled unit '{u}'", None, None, None
        if f == 1: return (lambda x: x), "as stated (arcsec)", None, None, None
        fx = lambda s: f"{s}*{_lit(f)}"
        return (lambda x: x * f), f"{u} -> arcsec", None, fx, fx
    if kind == "pc":
        # h-scaled lengths (cosmology H0 = 100 h km/s/Mpc), e.g. "h-1 kpc", "h^{-1} kpc", "kpc/h": divide by h
        hs = re.compile(r"(h\s*(\^\s*\{?\s*[-\u2212\u2013]\s*1\s*\}?|[-\u2212\u2013]\s*1|\u207b\u00b9)|/\s*h\b)")
        hfac = None
        if hs.search(u):
            hfac = d.get("_h") or H_LITTLE; u = re.sub(r"\s+", " ", hs.sub(" ", u)).strip()
            hsrc = "h stated in paper" if d.get("_h_stated") else "default h: no h value in paper"
        f = {"pc": 1, "mpc": 1e-3, "kpc": 1e3, "au": 4.8481e-6}.get(u)
        if f is None: return None, f"unhandled unit '{u}'", None, None, None
        if hfac is not None:
            fx = lambda s: f"{s}/{_lit(hfac)}*{_lit(f)}"
            return (lambda x: x / hfac * f), f"h^-1 {u} -> pc with h = {hfac:g} (h-scaled; {hsrc})", None, fx, fx
        if f == 1: return (lambda x: x), "as stated (pc)", None, None, None
        fx = lambda s: f"{s}*{_lit(f)}"
        return (lambda x: x * f), f"{u} -> pc", None, fx, fx
    if kind == "recip":   # mass ratio stated as > 1
        return (lambda x: 1.0 / x), "reciprocal of stated mass ratio (>1)", None, (lambda s: f"1/{s}"), None
    return (lambda x: x), "as stated", None, None, None

def _r(x, nd):
    if x is None: return None
    return round(x, nd) if nd is not None else float(f"{x:.4g}")

def convert(kind, d, z):
    """Apply BOBcat unit and error conventions to one stated quantity.
    -> dict(v, e, et, note, probs, vf, ef): numeric value/error (for checks and provenance) and the
    spreadsheet entries vf/ef, which are formulas showing the conversion when one was applied."""
    out = dict(v=None, e=None, et=None, note="", probs=[], vf=None, ef=None)
    d = dict(d); v, et = d.get("value"), d.get("error_type")
    lo, hi = d.get("range_low"), d.get("range_high")
    pre = []
    if v is not None and lo is not None and hi is not None and et not in ("Upper Limit", "Lower Limit") and lo <= v <= hi:
        lo_d, hi_d = v - lo, hi - v                      # best-fit value with a range
        if abs(hi_d - lo_d) <= 1e-9 * max(abs(v), 1):
            et = "Range"; pre.append("best-fit value with symmetric range -> Range")
        else:
            d["err_plus"], d["err_minus"], et = hi_d, lo_d, "Two sided"; lo = hi = None
            pre.append(f"best-fit value with asymmetric range {d.get('range_low'):g}-{d.get('range_high'):g} -> Two sided")
    if v is None and not (lo is not None and hi is not None and et in ("Range", "Representative", "Assumed")):
        return out
    f, desc, nd, fx, fe = _scalar_fn(kind, d, z)
    if f is None: out.update(note=desc, probs=[desc]); return out
    ep, em = d.get("err_plus"), d.get("err_minus"); probs = list(pre)
    if "WARNING" in desc: probs.append("REST FRAME BUT REDSHIFT UNKNOWN - value NOT converted to Earth frame")
    if "(h-scaled" in desc: probs.append("h-scaled length: " + desc[desc.find("with h ="):])
    q = (d.get("quote") or "").lower()
    if kind == "days_earth" and d.get("frame") == "observed" and re.search(r"rest[- ~]?frame", q):
        probs.append("quote says rest frame but frame=observed")
    X = (lambda s: fx(s)) if fx else (lambda s: s)
    formula = fx is not None
    def val(x): return ("=" + X(_lit(x))) if formula else _r(f(x), nd)
    # ---- ranges (Range, or a span given for an Assumed/Representative value)
    if lo is not None and hi is not None and (et == "Range" or v is None):
        a, b = sorted([f(lo), f(hi)])
        ea, eb = X(_lit(lo)), X(_lit(hi))
        mid, half = _r((a + b) / 2, nd), _r((b - a) / 2, nd)
        if et == "Range":
            vf = f"=(({ea})+({eb}))/2" if formula else mid
            ef = f"=ABS(({eb})-({ea}))/2" if formula else half
            out.update(v=mid, e=half, et="Range", vf=vf, ef=ef, note=f"{desc}; range {lo:g}-{hi:g} -> midpoint ± half-width", probs=probs)
        else:
            out.update(v=mid, et=et, vf=(f"=(({ea})+({eb}))/2" if formula else mid), note=f"{desc}; midpoint of {lo:g}-{hi:g}", probs=probs)
        return out
    if et == "Range": probs.append("Range without range_low/high")
    if et in ("Assumed", "Representative") and (ep is not None or em is not None):
        probs.append(f"{et} but numerical errors given -> classified from errors"); et = "Gaussian"
    if et in ("Gaussian", "Two sided"):
        if ep is None and em is None:
            probs.append(f"{et} but no errors given -> Representative"); et = "Representative"
        else:
            ep = ep if ep is not None else em; em = em if em is not None else ep
            if kind in ("log_mass", "recip") and v - em <= 0 and fx is not None:
                probs.append("lower error exceeds value; transformed error undefined")
                out.update(v=_r(f(v), nd), et=et, vf=val(v), note=desc, probs=probs); return out
            p_hi, p_lo = f(v + ep), f(v - em)                     # images of the interval ends
            decreasing = p_hi < p_lo
            up = max(p_hi, p_lo) - f(v); dn = f(v) - min(p_hi, p_lo)
            sym = abs(up - dn) <= 1e-6 * max(abs(up), abs(dn), 1e-300)
            upr, dnr = _r(up, nd), _r(dn, nd)
            # formula pieces for the upper / lower error in sheet units
            s_up_end, s_lo_end = (f"({_lit(v)}-{_lit(em)})", f"({_lit(v)}+{_lit(ep)})") if decreasing else \
                                 (f"({_lit(v)}+{_lit(ep)})", f"({_lit(v)}-{_lit(em)})")
            if fe is not None: e_up, e_dn = fe(_lit(ep if not decreasing else em)), fe(_lit(em if not decreasing else ep))
            else: e_up, e_dn = f"({X(s_up_end)})-({X(_lit(v))})", f"({X(_lit(v))})-({X(s_lo_end)})"
            if sym or upr == dnr:
                out.update(v=_r(f(v), nd), e=upr, et="Gaussian", vf=val(v), ef=("=" + e_up) if formula else upr, note=desc,
                           probs=probs + (["Two sided with equal errors -> Gaussian"] if et == "Two sided" else []))
                return out
            if formula:
                rd = (lambda s: f"ROUND({s},{nd if nd is not None else 4})")
                ef = f'="-"&{rd(e_dn)}&",+"&{rd(e_up)}'
            else:
                ef = f"-{dnr},+{upr}"
            note = desc + ("; symmetric error becomes asymmetric after conversion" if et == "Gaussian" else "")
            out.update(v=_r(f(v), nd), e=f"-{dnr},+{upr}", et="Two sided", vf=val(v), ef=ef, note=note, probs=probs)
            return out
    if et in ("Upper Limit", "Lower Limit", "Assumed", "Representative"):
        if ep is not None or em is not None: probs.append(f"{et} but errors were given (dropped)")
        out.update(v=_r(f(v), nd), et=et, vf=val(v), note=desc, probs=probs); return out
    probs.append(f"unknown error_type {et!r}")
    out.update(v=_r(f(v), nd), et=et, vf=val(v), note=desc, probs=probs); return out

def _range_ok(name, v, rng):
    if v is None or not isinstance(v, (int, float)): return ""
    base = re.sub(r"\d+$", "", name)
    checks = {"eccentricity": 0 <= v < 1, "q": 0 < v <= 1, "inclination": 0 <= v <= 90,
              "semi-major axis": 0 < v <= 1000, "separation": 0 < v <= 1000,
              "angular separation": v > 0, "paper redshift": v > 0}
    if base.startswith("log("): ok = 4 < v < 12
    elif "epoch" in base: ok = 40000 <= v <= MJD_MAX
    elif "period" in base or "frequency" in base: ok = v > 0
    else: ok = checks.get(base, True)
    return "" if ok else f"OUT OF RANGE ({rng})"

# ----------------------------------------------------------------- sheet assembly
EVID_ROWS = ["evidence type{}", "evidence type note{}", "evidence type waveband{}"]
PER_ROWS = ["observed period (earth frame){}", "observed period waveband{}", "observed period epoch{}"]

def _expand_template(tpl, n_evid, n_per=0):
    """Template rows; evidence sets 3..n and observed-period sets 3..m are appended at the END of the sheet
    (BOBcat convention), copying the metadata of the template's set-2 rows."""
    f = tpl["fields"].copy().reset_index(drop=True); extra = []
    for rows, n in ((EVID_ROWS, n_evid), (PER_ROWS, n_per)):
        s2 = f[f["Name"].isin([r.format(2) for r in rows])]
        for k in range(3, n + 1):
            blk = s2.copy(); blk["Name"] = blk["Name"].str.replace(r"2$", str(k), regex=True); extra.append(blk)
    return pd.concat([f, *extra], ignore_index=True) if extra else f

_APPROX = re.compile(r"\\approx|\\sim|\\simeq|~\s*\d|≈|∼|\babout\b|\broughly\b|\bof order\b", re.I)

def enforce_error_type(et, err, d, probs):
    """Deterministic BOBcat rules for a value without errors:
    cited from another paper -> Assumed; the paper's own value -> Representative (catch-all), unless the paper
    explicitly assumes it (Assumed kept)."""
    probs = list(probs)
    if err is not None or et in ("Upper Limit", "Lower Limit", "Range", None): return et, probs
    cited = bool((d.get("cited_from") or "").strip())
    assumed_words = re.search(r"assum|adopt|we (take|set|use|fix)|is taken|are taken|fixed (at|to)|set to|for simplicity"
                              r"|\bif\b|suppos|provided (that)?|in the case (of|that)", d.get("quote") or "", re.I)   # conditional = assumption
    new = "Assumed" if cited else ("Assumed" if et == "Assumed" and assumed_words else "Representative")
    if new != et: probs.append(f"error type {et} -> {new} by rule")
    return new, probs

def _reciprocal_q(d):
    """Mass ratio stated > 1 -> store 1/q (limits swap direction). Returns (d, kind, note)."""
    d = dict(d); v = d.get("value"); lo, hi = d.get("range_low"), d.get("range_high")
    big = (v is not None and v > 1) or (v is None and lo is not None and hi is not None and min(lo, hi) >= 1)
    if not big: return d, "plain", ""
    swap = {"Lower Limit": "Upper Limit", "Upper Limit": "Lower Limit"}
    d["error_type"] = swap.get(d.get("error_type"), d.get("error_type"))
    return d, "recip", "mass ratio stated > 1: reciprocal stored" + ("; limit direction swapped" if d["error_type"] in swap else "")

KIND = {"eccentricity": "plain", "m1": "log_mass", "m2": "log_mass", "total_mass": "log_mass",
        "chirp_mass": "log_mass", "reduced_mass": "log_mass", "mass_ratio_q": "plain",
        "orbital_inclination": "plain", "semi_major_axis": "pc", "separation": "pc", "angular_separation": "arcsec",
        "period_epoch": "mjd", "orbital_frequency": "plain", "orbital_period": "days_earth"}
# Orbit-size fields (BOBcat definitions, user-confirmed):
#   semi-major axis = half the longest diameter of the (elliptical) orbit; for a circular orbit it is the orbit's radius.
#   separation      = the current separation of the two black holes (observed projected or orbital); for a circular
#                     orbit it is the orbit's diameter, i.e. twice the semi-major axis.
#   Each is recorded as stated; neither is derived from the other.
ROWNAME = {"eccentricity": "eccentricity", "m1": "log(m1)", "m2": "log(m2)", "total_mass": "log(total mass)",
           "chirp_mass": "log(chirp mass)", "reduced_mass": "log(reduced mass)", "mass_ratio_q": "q",
           "orbital_inclination": "inclination", "semi_major_axis": "semi-major axis", "separation": "separation", "angular_separation": "angular separation",
           "period_epoch": "period epoch", "orbital_frequency": "orbital frequency (earth frame)",
           "orbital_period": "orbital period (earth frame)"}

def drop_derivable(m):
    """BOBcat: ingestion derives total/chirp/reduced mass and q from m1+m2, and frequency from period, so those are
    left empty when their inputs are filled (logged in the model's extraction notes)."""
    import copy
    m = _decode_fields(copy.deepcopy(m)) if "_decode_fields" in globals() else copy.deepcopy(m)
    m = {k: (v if not isinstance(v, str) or k not in QFIELDS else None) for k, v in m.items()} if "QFIELDS" in globals() else m
    has = lambda k: (m.get(k) or {}).get("value") is not None or (m.get(k) or {}).get("range_low") is not None
    dropped = []
    if has("m1") and has("m2"):
        for k in ("total_mass", "mass_ratio_q", "chirp_mass", "reduced_mass"):
            if has(k): dropped.append(k); m[k] = {"value": None}
    if has("orbital_period") and has("orbital_frequency"):
        dropped.append("orbital_frequency"); m["orbital_frequency"] = {"value": None}
    if dropped:
        m["extraction_notes"] = ((m.get("extraction_notes") or "") + f" [Dropped as derivable at ingestion: {', '.join(dropped)}]").strip()
    return m

_WB_PAT = [("gamma-ray", r"gamma|fermi|\bgev\b|\btev\b"), ("X-ray", r"x-?ray|\bxrt\b|kev|chandra|xmm|rosat"),
           ("UV", r"\buv\b|ultra-?violet|galex|uvot"), ("IR", r"infra-?red|\b[nm]?ir\b|wise|spitzer|\b[jhk]s?[- ]band\b|w[12]\b"),
           ("Radio", r"radio|ghz|mhz|vlbi|vlba|\bmm\b|cm\b"),
           ("Optical", r"optical|visible|\b[ugrizvbr]\b|[ugrizvbr][- ]band|ztf|crts|lincoln|catalina|asas|\bps1\b|pan-?starrs")]
def _norm_waveband(w, allowed):
    """Map free-text waveband wording (e.g. 'optical (V-band)', 'Swift/XRT 0.3-10 keV') onto the template list."""
    if not w: return w
    if w in allowed: return w
    hits = [b for b, pat in _WB_PAT if b in allowed and re.search(pat, str(w), re.I)]
    hits += [b for b in allowed if b.lower() == str(w).strip().lower() and b not in hits]
    return "; ".join(dict.fromkeys(hits)) if hits else w

def build_rows(ext, tpl, tex, paper_link, model=None):
    """-> list of (model_label, sheet_df, provenance_df, numeric_df).
    sheet_df: exact template layout with plain numbers (what is written); numeric_df: identical copy kept for
    compare_to_gold. The conversion formulas are recorded on the provenance sheet only."""
    out = []
    paper_h = find_little_h(tex)
    for m in ext["models"]:
        z = m.get("redshift") if m.get("redshift") is not None else ext.get("redshift")
        rows = {}
        def put(name, val, err=None, etype=None, quote="", note="", cited="", probs=(), vf=None, ef=None):
            rows[name] = dict(Value=val, Error=err, etype=etype, quote=quote or "", note=note or "", cited=cited or "",
                              probs=list(probs), vf=val if vf is None else vf, ef=err if ef is None else ef)
        def put_conv(name, c, d, extra_note=""):
            et, pr = enforce_error_type(c["et"], c["e"], d, c["probs"])
            put(name, c["v"], c["e"], et, d.get("quote"), "; ".join(x for x in [d.get("note"), c["note"], extra_note] if x),
                d.get("cited_from"), pr, c["vf"], c["ef"])
        put("paper link", paper_link)
        put("source nickname used in paper", re.sub("[\u2010-\u2015\u2212]", "-", re.sub(r"\s*\(.*?\)\s*$", "", (m.get("source_nickname") or ext.get("source_nickname") or ""))).strip())
        zq = m.get("redshift_quote") or ext.get("redshift_quote") or ""
        put("paper redshift", z, None, "Representative" if z is not None else None, quote=zq,
            note="redshift given in the paper; also used for rest -> Earth frame conversion" if z is not None else "")
        m = drop_derivable(m)
        for key, name in ROWNAME.items():
            d = dict(m.get(key) or {}); kind, xnote = KIND[key], ""
            if kind == "pc": d["_h"], d["_h_stated"] = paper_h
            if key == "mass_ratio_q": d, kind, xnote = _reciprocal_q(d)
            if key == "orbital_period" and d.get("value") is not None and d.get("frame") not in ("observed", "rest"):
                d["frame"] = "rest"; xnote = "frame not given by extractor: model orbital period taken as rest frame (verify)"
            if key == "period_epoch" and (m.get("orbital_period") or {}).get("value") is None \
               and (m.get("orbital_period") or {}).get("range_low") is None:
                continue                                    # period epoch belongs to the orbital period only
            if key == "period_epoch" and d.get("value") is None and d.get("range_low") is not None and d.get("range_high") is not None:
                d["error_type"] = "Range"
            c = convert(kind, d, z)
            if key == "period_epoch" and c["v"] is None and ext.get("_fallback_epoch"):
                fb = ext["_fallback_epoch"]
                put(name, fb["mjd"], None, "Representative", "", fb["note"], "", fb.get("probs", []), vf=fb.get("formula"))
                continue
            if c["v"] is None and not c["probs"]: continue
            put_conv(name, c, d, xnote)
        ev = [e for e in _coerce_list(m.get("evidence") or [], "evidence") if isinstance(e, dict)]   # tolerate JSON-string lists
        for k, e in enumerate(ev, 1):
            sfx = "" if k == 1 else str(k)
            src_ = "this paper" if e.get("from_this_paper") else "cited evidence"
            put(f"evidence type{sfx}", e["evidence_type"], quote=e.get("quote"), note=src_)
            put(f"evidence type note{sfx}", e["evidence_note"])
            put(f"evidence type waveband{sfx}", e["waveband"])
        pers = [p for p in _coerce_list(m.get("observed_periods") or [], "observed_periods") if isinstance(p, dict)]
        _exp = []                                   # one observed-period set per waveband (user rule: DB allows one band)
        for p in pers:
            _b = [b.strip() for b in str(_norm_waveband(p.get("waveband"), tpl["wavebands"]) or "").split(";") if b.strip() in tpl["wavebands"]]
            _exp += [dict(p, waveband=b, _wb_split_from=p.get("waveband")) for b in _b] if len(_b) >= 2 else [p]
        _seen, pers = set(), []
        for p in _exp:                              # drop exact duplicates (e.g. two sub-bands mapping to the same band)
            _k = (p.get("value"), str(p.get("unit")), p.get("frame"), _norm_waveband(p.get("waveband"), tpl["wavebands"]))
            if _k in _seen: continue
            _seen.add(_k); pers.append(p)
        for i, p in enumerate(pers, 1):
            sfx = "" if i == 1 else str(i)
            p = dict(p); pn = ""
            if p.get("frame") not in ("observed", "rest"):
                p["frame"] = "observed"; pn = "frame not given by extractor: taken as observed frame (verify)"
            put_conv(f"observed period (earth frame){sfx}", convert("days_earth", p, z), p, pn)
            wb_raw = p.get("_wb_split_from") or p.get("waveband"); wb = _norm_waveband(p.get("waveband"), tpl["wavebands"])
            put(f"observed period waveband{sfx}", wb, note=(f"as stated: {wb_raw}" + ("; split into one observed-period set per waveband"
                if p.get("_wb_split_from") else "") if wb_raw and wb != wb_raw else ""))
            pe = dict(p.get("epoch") or {})
            if not pe and p.get("epoch_mjd") is not None:          # older raw files
                pe = dict(value=p["epoch_mjd"], unit="MJD", error_type="Representative", note=p.get("epoch_as_stated", ""))
            if pe.get("value") is None and pe.get("range_low") is not None and pe.get("range_high") is not None:
                pe["error_type"] = "Range"
            pe.setdefault("error_type", "Representative")
            ce = convert("mjd", pe, z)
            if ce["v"] is not None:
                put_conv(f"observed period epoch{sfx}", ce, pe)
            elif ext.get("_fallback_epoch"):
                fb = ext["_fallback_epoch"]
                put(f"observed period epoch{sfx}", fb["mjd"], None, "Representative", "", fb["note"], "", fb.get("probs", []), vf=fb.get("formula"))
        for nm, key in (("Summary/notes on source", "summary"), ("Caveats", "caveats")):
            txt, pr = clip_text(m.get(key), model=model); put(nm, txt, probs=pr)

        f = _expand_template(tpl, len(ev), len(pers))
        for c_ in ["Value", "Error", "Error type"]: f[c_] = f[c_].astype(object)
        num = f.copy(); prov = []
        for i, r in f.iterrows():
            name = str(r["Name"]).strip(); d = rows.get(name)
            if d is None: continue
            f.at[i, "Value"] = num.at[i, "Value"] = d["Value"]          # plain numbers in the sheet (user convention)
            if d["Error"] is not None: f.at[i, "Error"] = num.at[i, "Error"] = d["Error"]
            if d["etype"] is not None: f.at[i, "Error type"] = num.at[i, "Error type"] = d["etype"]
            chk = list(d["probs"])
            if d["Value"] is not None and d["quote"]: chk.append(verify_quote(d["quote"], tex))
            elif d["Value"] is not None and d["etype"] is not None and not d["vf"] == ext.get("_fallback_epoch", {}).get("formula"):
                chk.append("NO SUPPORTING QUOTE - verify")
            rc = _range_ok(name, d["Value"] if isinstance(d["Value"], (int, float)) else None, r["Expected range"])
            if rc: chk.append(rc)
            if d["cited"]: chk.append(f"cited from {d['cited']}")
            if name.startswith("evidence type note"):
                t = rows.get(name.replace(" note", ""), {}).get("Value")
                chk.append("vocab OK" if d["Value"] in tpl["vocab"].get(t, []) else f"NOT IN VOCAB for {t}")
                if d["Value"] == "Discrete bursts with periodicity":
                    q = rows.get(name.replace(" note", ""), {}).get("quote", "")
                    if re.search(r"\bdips?\b|drop-?outs?\b|\bdeclines?\b|\bdecreases?\b", q, re.I) or not re.search(r"burst|flare|outburst", q, re.I):
                        chk.append("VERIFY: 'Discrete bursts' needs >=2 recurring bursts (not dips/interruptions)")
            if name.startswith("evidence type waveband") or name.startswith("observed period waveband"):
                bad = [w.strip() for w in str(d["Value"]).split(";") if w.strip() not in tpl["wavebands"]]
                chk.append("vocab OK" if not bad else f"NOT IN VOCAB: {bad}")
            formula = d["vf"] if isinstance(d["vf"], str) and d["vf"].startswith("=") else ""
            eformula = d["ef"] if isinstance(d["ef"], str) and str(d["ef"]).startswith("=") else ""
            prov.append({"Name": name, "Value": d["Value"], "Error": d["Error"], "Error type": d["etype"],
                         "Value formula": formula, "Error formula": eformula,
                         "Source quote": d["quote"], "Extractor note": d["note"], "Automated checks": "; ".join(chk)})
        if (m.get("extraction_notes") or "").strip():
            prov.append({"Name": "(extraction notes)", "Extractor note": m["extraction_notes"]})
        if (m.get("external_caveats") or "").strip():
            prov.append({"Name": "(external caveats - verify, not for sheet)", "Extractor note": m["external_caveats"]})
        out.append((m.get("model_label", "model"), f, pd.DataFrame(prov), num))
    return out

def write_xlsx(path, sheet, tpl, meta, review_log=()):
    """One model per workbook, laid out like the template: Sheet1 = model (plain numbers), Sheet2 = dropdown
    vocabulary, then provenance (quotes, notes, conversion formulas, checks), review_log and extraction_meta."""
    label, df, prov = sheet[0], sheet[1], sheet[2]
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE as _ILL
    _XB = re.compile("[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
    _cl = lambda x: _XB.sub("", _ILL.sub("", x)) if isinstance(x, str) else x
    df, prov = df.map(_cl), prov.map(_cl)
    meta = {k: _cl(v) for k, v in meta.items()}; review_log = [{k: _cl(v) for k, v in r.items()} for r in review_log]
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        df.to_excel(w, sheet_name="Sheet1", index=False)
        tpl["sheet2"].to_excel(w, sheet_name="Sheet2", index=False)
        prov.to_excel(w, sheet_name="provenance", index=False)
        pd.DataFrame(list(review_log) or [{}]).to_excel(w, sheet_name="review_log", index=False)
        pd.DataFrame(list(meta.items()), columns=["key", "value"]).to_excel(w, sheet_name="extraction_meta", index=False)
        for ws in w.book.worksheets:
            for col, wd in zip("ABCDEFGHIJK", [30, 10, 10, 30, 16, 14, 22, 18, 50, 40, 40]):
                ws.column_dimensions[col].width = wd

H_LITTLE = 0.7              # default h for h-scaled lengths when the paper gives no h value (user rule)

def find_little_h(tex):
    """h value a paper states for H0 = 100h km/s/Mpc (e.g. 'h = 0.72', 'h_{100} = 0.7' near 'H_0'/'Hubble').
    -> (h, stated): the stated value in [0.4, 1.0], else (H_LITTLE, False)."""
    t = tex or ""
    for m in re.finditer(r"(?<![A-Za-z\\])h(?:_\{?100\}?)?\s*(?:=|\u2261|\\equiv|\\simeq|\u2243)\s*(0\.\d{1,3}|1\.0+)(?!\d)", t):
        ctx = t[max(0, m.start() - 300): m.end() + 300]
        if re.search(r"H_?\{?0\}?|H\$?_0|H0|Hubble", ctx) and 0.4 <= float(m.group(1)) <= 1.0:
            return float(m.group(1)), True
    return H_LITTLE, False
BOBCAT_MIN_LOG_MASS = 6.0   # BOBcat definition: EACH black hole >= 1e6 Msun (1e6 itself counts; user rule)
BOBCAT_MAX_SEP_PC = 1000.0  # BOBcat definition: separation within 1 kpc

def bobcat_definition_check(num):
    """Check one model's converted values (numeric sheet: Name / Value / Error type) against the BOBcat binary
    definition: both BHs >= 1e6 Msun and separation <= 1 kpc. Limits only count when they rule the model out
    (an Upper Limit mass below 1e6, a Lower Limit separation above 1 kpc).
    -> (status, reasons); status is 'within BOBcat definition', 'OUTSIDE BOBcat definition' or 'not checkable'."""
    s = num.drop_duplicates("Name").set_index("Name")
    def get(n):
        if n not in s.index: return None, ""
        try: v = float(s.at[n, "Value"])
        except (TypeError, ValueError): return None, ""
        return (None, "") if math.isnan(v) else (v, str(s.at[n, "Error type"]))
    eps, reasons, checked = 1e-6, [], []
    lm1, t1 = get("log(m1)"); lm2, t2 = get("log(m2)"); lmt, tt = get("log(total mass)"); q, _ = get("q")
    masses = [(n, v, t) for n, v, t in (("m1", lm1, t1), ("m2", lm2, t2)) if v is not None]
    if q is not None and q > 0:                      # q = m2/m1 <= 1 (BOBcat convention)
        if lm1 is not None and lm2 is None: masses.append(("m2 (from m1 and q)", lm1 + math.log10(q), t1))
        elif lm2 is not None and lm1 is None: masses.append(("m1 (from m2 and q)", lm2 - math.log10(q), t2))
        elif lmt is not None and lm1 is None and lm2 is None:
            masses.append(("m1 (from total mass and q)", lmt - math.log10(1 + q), tt))
            masses.append(("m2 (from total mass and q)", lmt + math.log10(q / (1 + q)), tt))
    for n, v, t in masses:
        checked.append(n)
        if v < BOBCAT_MIN_LOG_MASS - eps and t != "Lower Limit":
            reasons.append(f"{n} = 10^{v:.2f} Msun < 1e6 Msun" + (" (upper limit)" if t == "Upper Limit" else ""))
    if lmt is not None and (lm1 is None or lm2 is None) and q is None:
        checked.append("total mass")
        if lmt < BOBCAT_MIN_LOG_MASS + math.log10(2) - eps and tt != "Lower Limit":
            reasons.append(f"total mass = 10^{lmt:.2f} Msun < 2e6 Msun, so at least one BH is below 1e6 Msun")
    for n in ("separation", "semi-major axis"):
        v, t = get(n)
        if v is None: continue
        checked.append(n)
        if v > BOBCAT_MAX_SEP_PC + eps and t != "Upper Limit":
            reasons.append(f"{n} = {v:g} pc > 1 kpc" + (" (lower limit)" if t == "Lower Limit" else ""))
    if reasons: return "OUTSIDE BOBcat definition", reasons
    if not checked: return "not checkable (no BH mass or separation filled)", []
    return "within BOBcat definition", ["checked: " + ", ".join(checked)]

def definition_role(role, reason, sheets):
    """Deterministic paper classification from the code definition check (user rule: a paper whose proposed binary does
    not meet the BOBcat definition is a non-binary paper). If EVERY model is OUTSIDE the definition and the LLM said
    binary_model or disproves_binary_evidence, the role becomes non_binary_alternative_only. Otherwise the LLM role stands
    (it may also be non-binary for papers that only propose a non-binary origin).
    -> (role, reason, log_entry or None)."""
    checks = [bobcat_definition_check(s[3]) for s in sheets]
    if sheets and all(c[0].startswith("OUTSIDE") for c in checks) and role in ("binary_model", "disproves_binary_evidence"):
        why = "; ".join(f"model {i + 1}: " + "; ".join(c[1]) for i, c in enumerate(checks))
        new = "non_binary_alternative_only"
        return new, (f"Set by code: every model is OUTSIDE the BOBcat definition ({why}). "
                     f"LLM classification was '{role}': {reason}"), \
               dict(model=None, item="paper_role", action="paper_role_by_definition", old_value=role, new_value=new,
                    reason=why, applied=True)
    return role, reason, None

def write_model_workbooks(out_dir, tag, sheets, tpl, meta, log):
    """Write one XLSX per model -> list of paths. A single model keeps the plain paper-title filename; several
    models are written as '<title> - model N.xlsx'. Each file carries only its own model's review-log entries
    (plus paper-level entries without a model index)."""
    paths = []
    for i, s in enumerate(sheets):
        name = f"{tag}.xlsx" if len(sheets) == 1 else f"{tag} - model {i + 1}.xlsx"
        chk, why = bobcat_definition_check(s[3])
        m_meta = dict(meta, model_index=i + 1, model_label=s[0], n_models=len(sheets),
                      bobcat_definition=chk, bobcat_definition_detail="; ".join(why))
        if chk.startswith("OUTSIDE"):
            m_meta["status"] = (str(meta.get("status") or "") + ". WARNING: this model is OUTSIDE the BOBcat binary "
                                "definition (" + "; ".join(why) + "): treat as a non-binary model")
        m_log = [l for l in log if l.get("model") in (i, None)]
        path = os.path.join(out_dir, name)
        write_xlsx(path, s, tpl, m_meta, m_log)
        paths.append(path)
    return paths

import threading as _threading_pdf
_PDF_LOCK = _threading_pdf.RLock()   # pdfium is NOT thread-safe: concurrent use (run_many) crashes the kernel (SIGSEGV)
_PDF_CACHE = {}

def pdf_to_text(pdf_path):
    """Plain text of a PDF (e.g. the published journal version), one form-feed between pages.
    Serialized with a lock (pdfium is not thread-safe) and cached per (path, size, mtime)."""
    import pypdfium2 as pdfium
    st = os.stat(pdf_path); key = (os.path.abspath(pdf_path), st.st_size, st.st_mtime)
    with _PDF_LOCK:
        if key in _PDF_CACHE: return _PDF_CACHE[key]
        doc = pdfium.PdfDocument(pdf_path)
        try:
            txt = "\f".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
        finally:
            doc.close()
        _PDF_CACHE[key] = txt
        return txt

