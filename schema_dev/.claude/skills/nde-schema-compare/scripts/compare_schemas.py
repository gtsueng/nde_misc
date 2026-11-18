"""
compare_schemas.py - Class-by-class changelog between two versions of the NDE schema.

    python compare_schemas.py [NEW] [OLD] [--out REPORT.md] [--json DIFF.json]

Defaults: NEW = NDE_schema_v2.json, OLD = NDE_schema_working.json (both in schema_dev).

For every class (rdfs:Class record) it compares the class's own $validation block:
  * properties added / removed
  * marginality  (required / recommended / optional lists)
  * cardinality  (owl:cardinality one / many on each property)
  * expected types (JSON Schema type/format, $ref'd definitions, array vs single,
                    enum / const values) for each property
plus class-level metadata (rdfs:subClassOf, schema:schemaVersion, dct:provenance),
changes to each class's `definitions` (nested object types), and changes to the
rdf:Property records (schema:domainIncludes / schema:rangeIncludes).

Only what is declared in each class's own $validation is compared (not what biothings
would merge in from a parent class); parent changes are reported separately.

Neither file is modified. If a file is not valid JSON it is parsed with the tolerant
parser from the sibling nde-schema-check skill and the report says so.
"""

import argparse
import collections
import datetime
import json
import os
import sys

LEVELS = ("required", "recommended", "optional")


# --------------------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------------------

def load(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    try:
        return json.loads(text), None
    except json.JSONDecodeError as e:
        first = f"line {e.lineno}: {e.msg}"
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(here, "..", "..", "nde-schema-check", "scripts"))
    try:
        import check_schema as cs
    except ImportError:
        raise SystemExit(f"{path} is not valid JSON ({first}) and the nde-schema-check skill is not available to repair it.")
    rep = cs.Report(text.splitlines(True))
    data, _, fixed = cs.check_syntax(text, rep)
    try:
        data = json.loads(fixed)
    except json.JSONDecodeError:
        pass
    errs = [f"line {i.line}: {i.message}" for i in rep.issues if i.severity == "ERROR"]
    return data, ("is not valid JSON; compared an in-memory copy with these syntax errors auto-repaired: "
                  + "; ".join(errs[:10]) + (" ..." if len(errs) > 10 else "")
                  + ". Run the nde-schema-check skill for details.")


def as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def ids_of(v):
    return [x["@id"] if isinstance(x, dict) else x for x in as_list(v) if isinstance(x, (dict, str))]


def index(data):
    classes, props = {}, {}
    for r in data.get("@graph", []):
        if not isinstance(r, dict):
            continue
        t = as_list(r.get("@type"))
        if "rdfs:Class" in t:
            classes[r.get("@id")] = r
        elif "rdf:Property" in t:
            props[r.get("@id")] = r
    return classes, props


# --------------------------------------------------------------------------------------
# type signatures
# --------------------------------------------------------------------------------------

def def_label(name, defs):
    """What a $ref'd definition accepts, e.g. 'object (ScholarlyArticle)'."""
    body = (defs or {}).get(name)
    if not isinstance(body, dict):
        return f"→{name} (missing definition)"
    sig = type_sig(body)  # no further expansion
    return " | ".join(sorted(sig)) if sig else f"object ({name})"


def type_sig(d, depth=0, defs=None):
    """Return a set of short strings describing the accepted value shapes.
    With `defs`, $refs are resolved to what the definition accepts (so a renamed or
    moved definition with the same type compares equal); without, they appear as →name."""
    if not isinstance(d, dict) or depth > 6:
        return set()
    out = set()
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in as_list(d.get(key)):
            out |= type_sig(sub, depth + 1, defs)
    if isinstance(d.get("$ref"), str):
        name = d["$ref"].split("/")[-1]
        out.add(def_label(name, defs) if defs is not None else "→" + name)
    t = d.get("type")
    for tt in as_list(t):
        if tt == "array":
            inner = type_sig(d.get("items") or {}, depth + 1, defs) or {"any"}
            out |= {f"array of {i}" for i in inner}
        elif tt == "object":
            label = d.get("@type")
            out.add(f"object ({label})" if label else "object")
        elif isinstance(tt, str):
            fmt = d.get("format")
            out.add(f"{tt} ({fmt})" if fmt else tt)
    if not out and ("enum" in d or "const" in d):
        out.add("enumerated value")
    return out


def value_constraints(d, depth=0):
    """enum / const values anywhere in a property definition (not inside $refs)."""
    vals = set()
    if not isinstance(d, dict) or depth > 6:
        return vals
    for v in as_list(d.get("enum")):
        vals.add(json.dumps(v) if not isinstance(v, str) else v)
    if "const" in d:
        c = d["const"]
        vals.add(json.dumps(c) if not isinstance(c, str) else c)
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in as_list(d.get(key)):
            vals |= value_constraints(sub, depth + 1)
    if isinstance(d.get("items"), dict):
        vals |= value_constraints(d["items"], depth + 1)
    return vals


def vocab(d, depth=0):
    """Ontology vocabulary restrictions (NDE 'vocabulary' keyword)."""
    out = set()
    if not isinstance(d, dict) or depth > 6:
        return out
    v = d.get("vocabulary")
    if isinstance(v, dict):
        for k in ("ontology", "children_of"):
            for x in as_list(v.get(k)):
                out.add(f"{k}:{x}")
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in as_list(d.get(key)):
            out |= vocab(sub, depth + 1)
    if isinstance(d.get("items"), dict):
        out |= vocab(d["items"], depth + 1)
    return out


def levels_of(val, ws=None):
    """name -> required/recommended/optional. Names are whitespace-stripped; names that had
    stray whitespace are added to the `ws` set if given."""
    m = {}
    for lvl in LEVELS:
        for n in as_list(val.get(lvl)):
            if not isinstance(n, str):
                continue
            if n != n.strip() and ws is not None:
                ws.add(n)
            m.setdefault(n.strip(), lvl)
    return m


def object_bodies(d, defs, depth=0):
    """label -> property names, for every object a property accepts (inline or via $ref)."""
    out = {}
    if not isinstance(d, dict) or depth > 6:
        return out
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in as_list(d.get(key)):
            out.update(object_bodies(sub, defs, depth + 1))
    if isinstance(d.get("items"), dict):
        out.update(object_bodies(d["items"], defs, depth + 1))
    if isinstance(d.get("$ref"), str):
        name = d["$ref"].split("/")[-1]
        body = (defs or {}).get(name)
        if isinstance(body, dict) and isinstance(body.get("properties"), dict):
            out[def_label(name, defs)] = (set(body["properties"]), f"definitions/{name}")
    elif isinstance(d.get("properties"), dict):
        lab = d.get("@type") or "object"
        out[f"object ({lab})" if d.get("@type") else "object"] = (set(d["properties"]), "inline")
    return out


def ref_names(d, depth=0):
    out = set()
    if not isinstance(d, dict) or depth > 6:
        return out
    if isinstance(d.get("$ref"), str):
        out.add(d["$ref"].split("/")[-1])
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in as_list(d.get(key)):
            out |= ref_names(sub, depth + 1)
    if isinstance(d.get("items"), dict):
        out |= ref_names(d["items"], depth + 1)
    return out


# --------------------------------------------------------------------------------------
# diffing
# --------------------------------------------------------------------------------------

def diff_sets(new, old):
    return sorted(new - old), sorted(old - new)


def compare_definition(name, nd, od, ndefs=None, odefs=None):
    """Short list of changes inside one $validation definition (a nested object type)."""
    ch = []
    np_, op_ = (nd.get("properties") or {}), (od.get("properties") or {})
    add, rem = diff_sets(set(np_), set(op_))
    nws, ows = set(), set()
    nl, ol = levels_of(nd, nws), levels_of(od, ows)
    if add:
        ch.append("added " + ", ".join(f"`{x}` ({nl.get(x, 'unlisted')})" for x in add))
    if rem:
        ch.append("removed " + ", ".join(f"`{x}` (was {ol.get(x, 'unlisted')})" for x in rem))
    for p in sorted(set(np_) & set(op_)):
        if nl.get(p) != ol.get(p):
            ch.append(f"`{p}` marginality {ol.get(p, 'unlisted')} → {nl.get(p, 'unlisted')}")
        a, b = np_[p] if isinstance(np_[p], dict) else {}, op_[p] if isinstance(op_[p], dict) else {}
        if a.get("owl:cardinality") != b.get("owl:cardinality"):
            ch.append(f"`{p}` cardinality {b.get('owl:cardinality', 'unspecified')} → {a.get('owl:cardinality', 'unspecified')}")
        ts_n, ts_o = type_sig(a, defs=ndefs), type_sig(b, defs=odefs)
        if ts_n != ts_o:
            ch.append(f"`{p}` type {fmt_set(ts_o)} → {fmt_set(ts_n)}")
    for p in sorted(ows):
        ch.append(f"fixed stray whitespace in marginality list name `\"{p}\"` (did not match the property before)")
    for p in sorted((set(nl) | set(ol)) - set(np_) - set(op_)):
        if nl.get(p) != ol.get(p):
            ch.append(f"`{p}` was listed as {ol.get(p, 'unlisted')} but never defined in properties → "
                      + (nl[p] if p in nl else "removed from the list"))
    if type_sig(nd) != type_sig(od):
        ch.append(f"definition type {fmt_set(type_sig(od))} → {fmt_set(type_sig(nd))}")
    return ch


def fmt_set(s):
    return " \\| ".join(sorted(s)) if s else "—"


def compare_class(cid, new, old):
    """Returns a dict of changes for one class present in both versions."""
    nv, ov = new.get("$validation") or {}, old.get("$validation") or {}
    np_, op_ = nv.get("properties") or {}, ov.get("properties") or {}
    nws, ows = set(), set()
    nl, ol = levels_of(nv, nws), levels_of(ov, ows)
    ndefs, odefs = nv.get("definitions") or {}, ov.get("definitions") or {}
    c = {"meta": [], "added": [], "removed": [], "marginality": [], "cardinality": [], "types": [],
         "refactors": [], "values": [], "definitions": []}

    # class-level metadata
    for key, label in (("rdfs:subClassOf", "Parent class (rdfs:subClassOf)"),
                       ("schema:schemaVersion", "Version (schema:schemaVersion)"),
                       ("dct:provenance", "Provenance note (dct:provenance)")):
        a, b = new.get(key), old.get(key)
        if key == "rdfs:subClassOf":
            a, b = ids_of(a), ids_of(b)
        if a != b:
            c["meta"].append((label, b, a))

    add, rem = diff_sets(set(np_), set(op_))
    for p in add:
        c["added"].append((p, nl.get(p, "unlisted"), np_[p].get("owl:cardinality", "unspecified") if isinstance(np_[p], dict) else "?",
                           fmt_set(type_sig(np_[p], defs=ndefs))))
    for p in rem:
        c["removed"].append((p, ol.get(p, "unlisted"), op_[p].get("owl:cardinality", "unspecified") if isinstance(op_[p], dict) else "?",
                             fmt_set(type_sig(op_[p], defs=odefs))))
    for p in sorted(ows):
        c["marginality"].append((f"\"{p}\" → \"{p.strip()}\"", "name had stray whitespace (matched nothing)",
                                 f"fixed, now {nl.get(p.strip(), 'unlisted')}"))

    for p in sorted(set(np_) & set(op_)):
        if nl.get(p) != ol.get(p):
            c["marginality"].append((p, ol.get(p, "unlisted"), nl.get(p, "unlisted")))
        nd, od = np_[p] if isinstance(np_[p], dict) else {}, op_[p] if isinstance(op_[p], dict) else {}
        if nd.get("owl:cardinality") != od.get("owl:cardinality"):
            c["cardinality"].append((p, od.get("owl:cardinality", "unspecified"), nd.get("owl:cardinality", "unspecified")))
        ts_n, ts_o = type_sig(nd, defs=ndefs), type_sig(od, defs=odefs)
        raw_n, raw_o = type_sig(nd), type_sig(od)
        # property-level differences inside the objects this property accepts
        bn, bo = object_bodies(nd, ndefs), object_bodies(od, odefs)
        body_notes = []
        for lab in sorted(set(bn) & set(bo)):
            a, r = diff_sets(bn[lab][0], bo[lab][0])
            if a or r:
                body_notes.append(f"{lab} " + "; ".join(
                    ([f"adds {bullets(a)}"] if a else []) + ([f"drops {bullets(r)}"] if r else [])))
        if ts_n != ts_o:
            a, r = diff_sets(ts_n, ts_o)
            c["types"].append((p, fmt_set(ts_o), fmt_set(ts_n), a, r))
        elif raw_n != raw_o or body_notes:
            rn, ro = ref_names(nd), ref_names(od)
            how = []
            if ro - rn or rn - ro:
                how.append(f"definition {bullets(sorted(ro - rn)) or 'inline object'} → {bullets(sorted(rn - ro)) or 'inline object'}")
            c["refactors"].append((p, "; ".join(how + body_notes) or "restructured, same accepted types"))
        va, vr = diff_sets(value_constraints(nd), value_constraints(od))
        qa, qr = diff_sets(vocab(nd), vocab(od))
        if va or vr or qa or qr:
            c["values"].append((p, va, vr, qa, qr))
    # names listed in marginality lists that are not properties (dangling) - only report changes
    for p in sorted((set(nl) | set(ol)) - set(np_) - set(op_)):
        if nl.get(p) != ol.get(p):
            c["marginality"].append((p, f"listed as {ol[p]} but never defined in properties" if p in ol else "unlisted",
                                     nl.get(p, "removed from the list") if p in nl else "removed from the list"))

    nd_, od_ = ndefs, odefs
    dadd, drem = diff_sets(set(nd_), set(od_))
    # rename detection: a removed and an added definition that accept the same object type
    renamed = {}
    for d in list(dadd):
        for e in list(drem):
            if e in renamed.values():
                continue
            if type_sig(nd_[d]) == type_sig(od_[e]) and type_sig(nd_[d]):
                renamed[d] = e
                break
    for d, e in renamed.items():
        ch = compare_definition(d, nd_[d] or {}, od_[e] or {}, ndefs, odefs)
        c["definitions"].append((f"{e}` → `{d}", ["renamed"] + ch))
    for d in dadd:
        if d not in renamed:
            c["definitions"].append((d, ["new definition: " + fmt_set(type_sig(nd_[d]))]))
    for d in drem:
        if d not in renamed.values():
            c["definitions"].append((d, ["definition removed"]))
    for d in sorted(set(nd_) & set(od_)):
        ch = compare_definition(d, nd_[d] or {}, od_[d] or {}, ndefs, odefs)
        if ch:
            c["definitions"].append((d, ch))
    return c


def compare_property_records(nprops, oprops):
    out = {"added": [], "removed": [], "changed": []}
    for p in sorted(set(nprops) - set(oprops)):
        r = nprops[p]
        out["added"].append((p, ids_of(r.get("schema:domainIncludes")), ids_of(r.get("schema:rangeIncludes"))))
    for p in sorted(set(oprops) - set(nprops)):
        r = oprops[p]
        out["removed"].append((p, ids_of(r.get("schema:domainIncludes")), ids_of(r.get("schema:rangeIncludes"))))
    for p in sorted(set(nprops) & set(oprops)):
        n, o = nprops[p], oprops[p]
        rows = []
        for key, label in (("schema:domainIncludes", "domain"), ("schema:rangeIncludes", "range")):
            a, r = diff_sets(set(ids_of(n.get(key))), set(ids_of(o.get(key))))
            if a or r:
                rows.append((label, a, r))
        if rows:
            out["changed"].append((p, rows))
    return out


# --------------------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------------------

def bullets(items):
    return ", ".join(f"`{x}`" for x in items)


def render(new_path, old_path, new_note, old_note, nclasses, oclasses, results, prec):
    L = []
    w = L.append
    w("# NDE schema changelog")
    w("")
    w(f"- **New:** `{os.path.basename(new_path)}`")
    w(f"- **Old:** `{os.path.basename(old_path)}`")
    w(f"- Generated: {datetime.datetime.now():%Y-%m-%d %H:%M}")
    w("- Scope: properties declared in each class's own `$validation` block (inherited properties merged in by "
      "biothings are not counted, but parent-class changes are listed).")
    for label, note in (("New", new_note), ("Old", old_note)):
        if note:
            w(f"- ⚠ {label} file {note}")
    w("")
    w("## Summary")
    w("")
    w("<!-- HOLISTIC SUMMARY: written by Claude after reading the sections below -->")
    w("")

    # overview table
    w("## Overview by class")
    w("")
    w("| Class | Props (old → new) | Added | Removed | Marginality | Cardinality | Types | Restructured | Values/vocab | Definitions |")
    w("|---|---|---|---|---|---|---|---|---|---|")
    for cid in sorted(set(nclasses) | set(oclasses), key=lambda x: (x not in oclasses, x not in nclasses, x)):
        if cid not in oclasses:
            n = len((nclasses[cid].get("$validation") or {}).get("properties") or {})
            w(f"| {cid} | new class ({n}) | | | | | | | | |")
            continue
        if cid not in nclasses:
            n = len((oclasses[cid].get("$validation") or {}).get("properties") or {})
            w(f"| {cid} | removed ({n}) | | | | | | | | |")
            continue
        c = results[cid]
        no = len((oclasses[cid].get("$validation") or {}).get("properties") or {})
        nn = len((nclasses[cid].get("$validation") or {}).get("properties") or {})
        w(f"| {cid} | {no} → {nn} | {len(c['added'])} | {len(c['removed'])} | {len(c['marginality'])} | "
          f"{len(c['cardinality'])} | {len(c['types'])} | {len(c['refactors'])} | {len(c['values'])} | {len(c['definitions'])} |")
    w("")

    # cross-class patterns
    pat = collections.defaultdict(list)
    for cid, c in results.items():
        for p, *_ in c["added"]:
            pat[("added", p)].append(cid)
        for p, *_ in c["removed"]:
            pat[("removed", p)].append(cid)
        for p, o, n in c["marginality"]:
            pat[(f"marginality {o} → {n}", p)].append(cid)
        for p, o, n in c["cardinality"]:
            pat[(f"cardinality {o} → {n}", p)].append(cid)
        for p, o, n, *_ in c["types"]:
            pat[(f"type {o} → {n}", p)].append(cid)
        for p, how in c["refactors"]:
            pat[(f"restructured: {how}", p)].append(cid)
        for d, chs in c["definitions"]:
            for ch in chs:
                pat[(f"definition `{d}`: {ch}", "")].append(cid)
    multi = [(k, v) for k, v in pat.items() if len(v) > 1]
    if multi:
        w("## Changes shared by several classes")
        w("")
        for (kind, p), cids in sorted(multi, key=lambda kv: (-len(kv[1]), kv[0])):
            short = [c.split(":")[-1] for c in cids]
            w(f"- {('`' + p + '` ' if p else '')}{kind} — {len(cids)} classes: {', '.join(short)}")
        w("")

    w("## Class-by-class changelog")
    w("")
    for cid in sorted(set(nclasses) | set(oclasses), key=lambda x: (x not in oclasses, x not in nclasses, x)):
        w(f"### {cid}")
        w("")
        if cid not in oclasses:
            v = nclasses[cid].get("$validation") or {}
            props = v.get("properties") or {}
            lv = levels_of(v)
            w(f"**New class.** Parent: {bullets(ids_of(nclasses[cid].get('rdfs:subClassOf')))}. {len(props)} properties:")
            w("")
            w("| Property | Marginality | Cardinality | Expected type |")
            w("|---|---|---|---|")
            for p, d in props.items():
                d = d if isinstance(d, dict) else {}
                w(f"| `{p}` | {lv.get(p, 'unlisted')} | {d.get('owl:cardinality', 'unspecified')} | {fmt_set(type_sig(d))} |")
            w("")
            continue
        if cid not in nclasses:
            w("**Class removed.**")
            w("")
            continue
        c = results[cid]
        if not any(c.values()):
            w("No changes.")
            w("")
            continue
        for label, o, n in c["meta"]:
            show = lambda v: ", ".join(map(str, v)) if isinstance(v, list) else ("—" if v is None else str(v))
            w(f"- **{label}:** {show(o)} → **{show(n)}**")
        if c["meta"]:
            w("")
        if c["added"]:
            w("#### Added properties")
            w("")
            w("| Property | Marginality | Cardinality | Expected type |")
            w("|---|---|---|---|")
            for p, lv, card, ts in c["added"]:
                w(f"| `{p}` | {lv} | {card} | {ts} |")
            w("")
        if c["removed"]:
            w("#### Removed properties")
            w("")
            w("| Property | Was marginality | Was cardinality | Was type |")
            w("|---|---|---|---|")
            for p, lv, card, ts in c["removed"]:
                w(f"| `{p}` | {lv} | {card} | {ts} |")
            w("")
        if c["marginality"] or c["cardinality"]:
            w("#### Marginality / cardinality changes")
            w("")
            w("| Property | Change |")
            w("|---|---|")
            for p, o, n in c["marginality"]:
                w(f"| `{p}` | marginality: {o} → **{n}** |")
            for p, o, n in c["cardinality"]:
                w(f"| `{p}` | cardinality: {o} → **{n}** |")
            w("")
        if c["types"]:
            w("#### Expected-type changes")
            w("")
            w("| Property | Old | New |")
            w("|---|---|---|")
            for p, o, n, a, r in c["types"]:
                w(f"| `{p}` | {o} | **{n}** |")
            w("")
        if c["refactors"]:
            w("#### Restructured / nested-object changes (top-level expected type unchanged)")
            w("")
            for p, how in c["refactors"]:
                w(f"- `{p}` — {how}")
            w("")
        if c["values"]:
            w("#### Allowed values / vocabulary changes")
            w("")
            for p, va, vr, qa, qr in c["values"]:
                parts = []
                if va:
                    parts.append("values added: " + bullets(va))
                if vr:
                    parts.append("values removed: " + bullets(vr))
                if qa:
                    parts.append("vocabulary added: " + bullets(qa))
                if qr:
                    parts.append("vocabulary removed: " + bullets(qr))
                w(f"- `{p}` — " + "; ".join(parts))
            w("")
        if c["definitions"]:
            w("#### Nested definition changes (`$validation.definitions`)")
            w("")
            for d, chs in c["definitions"]:
                w(f"- `{d}`: " + "; ".join(chs))
            w("")

    w("## Property records (`rdf:Property`)")
    w("")
    if not any(prec.values()):
        w("No changes.")
    for p, dom, rng in prec["added"]:
        w(f"- **Added** `{p}` — domain: {bullets(dom) or '—'}; range: {bullets(rng) or '—'}")
    for p, dom, rng in prec["removed"]:
        w(f"- **Removed** `{p}` — was domain: {bullets(dom) or '—'}; range: {bullets(rng) or '—'}")
    for p, rows in prec["changed"]:
        parts = []
        for label, a, r in rows:
            if a:
                parts.append(f"{label} + {bullets(a)}")
            if r:
                parts.append(f"{label} − {bullets(r)}")
        w(f"- **Changed** `{p}` — " + "; ".join(parts))
    w("")
    w("---")
    w("Legend: *marginality* = required / recommended / optional (\"unlisted\" = in `properties` but in none of the "
      "three lists). *cardinality* = `owl:cardinality` (one / many). *Expected type*: JSON Schema type (format), "
      "`$ref`s are resolved to the object type they define, `array of X` = list form; alternatives separated by `|`. "
      "*Restructured* = the property still accepts the same kinds of value, but the definition it points to was "
      "renamed / moved, or fields inside the nested object were added or dropped.")
    return "\n".join(L)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, "..", "..", "..", ".."))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("new", nargs="?", default=os.path.join(root, "NDE_schema_v2.json"))
    ap.add_argument("old", nargs="?", default=os.path.join(root, "NDE_schema_working.json"))
    ap.add_argument("--out", help="Markdown report (default: schema_changelog_<new>_vs_<old>.md next to NEW)")
    ap.add_argument("--json", help="Also write the raw diff as JSON")
    a = ap.parse_args()

    new, new_note = load(a.new)
    old, old_note = load(a.old)
    nclasses, nprops = index(new)
    oclasses, oprops = index(old)
    results = {cid: compare_class(cid, nclasses[cid], oclasses[cid]) for cid in nclasses if cid in oclasses}
    prec = compare_property_records(nprops, oprops)

    md = render(a.new, a.old, new_note, old_note, nclasses, oclasses, results, prec)
    stem = lambda p: os.path.splitext(os.path.basename(p))[0]
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.new)), f"schema_changelog_{stem(a.new)}_vs_{stem(a.old)}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({"classes": results, "property_records": prec}, f, indent=1, default=list)

    for label, note in (("NEW", new_note), ("OLD", old_note)):
        if note:
            print(f"WARNING {label} file {note}")
    for cid in sorted(set(nclasses) | set(oclasses)):
        if cid not in oclasses:
            print(f"{cid}: NEW CLASS")
        elif cid not in nclasses:
            print(f"{cid}: REMOVED")
        else:
            c = results[cid]
            print(f"{cid}: +{len(c['added'])} -{len(c['removed'])} marg {len(c['marginality'])} card {len(c['cardinality'])} "
                  f"types {len(c['types'])} values {len(c['values'])} defs {len(c['definitions'])}"
                  + (" parent changed" if any(m[0].startswith("Parent") for m in c["meta"]) else ""))
    print(f"property records: +{len(prec['added'])} -{len(prec['removed'])} ~{len(prec['changed'])}")
    print(f"Report written to {out}")


if __name__ == "__main__":
    main()
