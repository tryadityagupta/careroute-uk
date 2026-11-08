"""
scripts/import_nhs_directory.py — build data/uk/nhs/directory.json from NHS
open data, so CareRoute finds REAL pharmacies anywhere in England.

    python -m scripts.import_nhs_directory \
        --pharmacies downloads/pharmacies.csv \
        --postcodes-io

Inputs (all free, Open Government Licence v3.0):

  --pharmacies FILE...   pharmacy list(s). Either the NHSBSA "contractor
                         details" / consolidated pharmaceutical list (has a
                         header row), or an ODS export with headers. Columns
                         are matched by name; see ALIASES below.
  --gp-practices FILE    optional: ODS GP practices (epraccur, headerless
                         27-column layout, or an export with headers).
  --postcodes FILE       ONS Postcode Directory CSV (columns pcds, lat, long,
                         ctry), or any CSV with postcode + lat/long columns.
  --postcodes-io         or look postcodes up on postcodes.io (free, no key;
                         cached in data/uk/nhs/postcode-cache.json).

What is kept: open (not closed) services in England. For pharmacies, also
not distance-selling (internet-only, can't be visited), not private, not
out-of-hours-only contractors, when the file has those columns.

What is NOT in these registers, and stays None (unknown) in the output:
opening hours, Pharmacy First participation, walk-in. CareRoute tells the
patient to check those with the pharmacy; it never guesses them.

Organisations only: no named clinicians are read or written.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "data" / "uk" / "nhs" / "directory.json"
DEFAULT_CACHE = ROOT / "data" / "uk" / "nhs" / "postcode-cache.json"
OGL = "Contains public sector information licensed under the Open Government Licence v3.0."

# Normalised header name (lowercase, non-alphanumerics -> "_") -> field.
# First match wins, so trading names beat legal contractor names.
ALIASES = {
    "code": ["ods_code", "organisation_code", "org_code", "contractor_code", "pharmacy_ods_code",
             "pharmacy_ods_code_f_code", "pharmacy_code", "f_code", "code"],
    "name": ["trading_name", "pharmacy_trading_name", "organisation_name", "pharmacy_name",
             "name", "contractor_name"],
    "postcode": ["postcode", "post_code", "pcode"],
    "phone": ["telephone", "telephone_number", "contact_telephone_number", "phone"],
    "close_date": ["close_date", "closed_date", "end_date"],
    "status": ["status", "status_code", "organisation_status"],
    "account_type": ["dispenser_account_type", "account_type"],
    "private": ["private_contractor"],
    "out_of_hours": ["out_of_hrs_disp_contractor", "out_of_hours_contractor", "out_of_hours"],
    "dsp": ["dsp", "distance_selling", "distance_selling_pharmacy", "is_dsp",
            "distance_selling_contractor"],
    "setting": ["prescribing_setting"],
}
ADDRESS_ALIASES = [["address_line_1", "address1", "address_1", "address_line1", "address"],
                   ["address_line_2", "address2", "address_2", "address_line2"],
                   ["address_line_3", "address3", "address_3", "address_line3"],
                   ["address_line_4", "address4", "address_4", "address_line4"],
                   ["address_line_5", "address5", "address_5", "address_line5"]]

# The classic ODS headerless layout (epraccur and siblings), 0-based.
ODS_HEADERLESS = {"code": 0, "name": 1, "address": [4, 5, 6, 7, 8], "postcode": 9,
                  "close_date": 11, "status": 12, "phone": 17, "setting": 25}
ODS_GP_SETTING = "4"                 # prescribing setting 4 = GP practice

KEEP_UPPER = {"UK", "NHS", "GP", "LLP", "PLC", "PCN", "CIC", "LPS", "UTC", "BMA", "I", "II",
              "III", "IV", "NW", "NE", "SW", "SE", "W1", "EC1", "WC1"}
KEEP_LOWER = {"and", "of", "the", "at", "on", "in", "upon", "by"}
YES = {"y", "yes", "true", "1"}


class ImportError_(Exception):
    pass


# ---------------------------------------------------------------- reading
def norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (header or "").strip().lower()).strip("_")


def read_rows(path: Path) -> list[list[str]]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    return [row for row in csv.reader(text.splitlines()) if any(c.strip() for c in row)]


def column_map(header: list[str]) -> dict | None:
    """Map our fields to column indexes, or None if this isn't a header row."""
    names = [norm(h) for h in header]
    found: dict = {}
    for field, aliases in ALIASES.items():
        for a in aliases:
            if a in names:
                found[field] = names.index(a)
                break
    addr = []
    for aliases in ADDRESS_ALIASES:
        idx = next((names.index(a) for a in aliases if a in names), None)
        if idx is not None:
            addr.append(idx)
    found["address"] = addr
    if "code" in found and "postcode" in found:
        return found
    return None


def records_from(path: Path, *, expect_header: bool | None = None) -> tuple[list[dict], dict]:
    """Rows as dicts with our field names. Returns (records, info)."""
    rows = read_rows(path)
    if not rows:
        raise ImportError_(f"{path.name}: the file is empty.")
    cmap = column_map(rows[0]) if expect_header is not False else None
    if cmap:
        body, layout = rows[1:], "header"
    elif expect_header is True:
        raise ImportError_(
            f"{path.name}: couldn't find an organisation code and a postcode column.\n"
            f"Headers found: {', '.join(rows[0])}\n"
            "Add the right names to ALIASES in scripts/import_nhs_directory.py.")
    elif len(rows[0]) >= 13 and tidy_postcode(rows[0][ODS_HEADERLESS["postcode"]]):
        # Headerless only if the first row really is data: a postcode where the
        # ODS layout puts it. Otherwise it's a header we don't recognise.
        cmap, body, layout = ODS_HEADERLESS, rows, "ods-headerless"
    else:
        raise ImportError_(
            f"{path.name}: couldn't find an organisation code and a postcode column.\n"
            f"Headers found: {', '.join(rows[0])}\n"
            "Add the right names to ALIASES in scripts/import_nhs_directory.py, or send "
            "this message to whoever maintains the importer.")

    def cell(row, i):
        return row[i].strip() if i is not None and i < len(row) else ""

    out = []
    for row in body:
        r = {f: cell(row, cmap.get(f)) for f in ALIASES if f in cmap}
        r["address_lines"] = [cell(row, i) for i in cmap.get("address", []) if cell(row, i)]
        out.append(r)
    info = {"file": path.name, "layout": layout, "rows": len(out),
            "nhsbsa_like": layout == "header" and "account_type" in cmap}
    return out, info


# ---------------------------------------------------------------- cleaning
def tidy_postcode(pc: str) -> str | None:
    s = re.sub(r"\s+", "", (pc or "").upper())
    if not re.fullmatch(r"[A-Z]{1,2}\d[A-Z\d]?\d[A-Z]{2}", s):
        return None
    return f"{s[:-3]} {s[-3:]}"


def nice(text: str) -> str:
    """'BOOTS UK LIMITED' -> 'Boots UK Limited'. Registers are upper case."""
    words = []
    for i, w in enumerate((text or "").split()):
        bare = re.sub(r"[^A-Za-z0-9]", "", w).upper()
        if bare in KEEP_UPPER or any(ch.isdigit() for ch in w):
            words.append(w.upper())
        elif i and w.lower() in KEEP_LOWER:
            words.append(w.lower())
        else:
            words.append("-".join(p[:1].upper() + p[1:].lower() for p in w.split("-")))
    return " ".join(words)


def is_open(r: dict) -> bool:
    status = (r.get("status") or "").strip().upper()
    if status and status not in ("A", "ACTIVE", "OPEN"):
        return False
    return not (r.get("close_date") or "").strip()


def pharmacy_skip_reason(r: dict) -> str | None:
    if not is_open(r):
        return "closed"
    acct = (r.get("account_type") or "").lower()
    if acct and ("pharmacy" not in acct or "appliance" in acct):
        return "not a community pharmacy"
    if acct and "english" not in acct and ("welsh" in acct or "wales" in acct):
        return "outside England"
    if (r.get("private") or "").lower() in YES:
        return "private contractor"
    if (r.get("out_of_hours") or "").lower() in YES:
        return "out-of-hours contractor"
    if (r.get("dsp") or "").lower() in YES:
        return "distance selling (internet only)"
    return None


def gp_skip_reason(r: dict) -> str | None:
    if not is_open(r):
        return "closed"
    if r.get("setting") and r["setting"] != ODS_GP_SETTING:
        return "not a GP practice"
    return None


# ---------------------------------------------------------------- postcodes
class OnspdLookup:
    """Stream an ONS Postcode Directory CSV, keeping only the postcodes we need."""

    def __init__(self, path: Path):
        self.path = path

    def lookup(self, postcodes: set[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        with open(self.path, newline="", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.reader(f)
            names = [norm(h) for h in next(reader)]

            def col(*options):
                return next((names.index(o) for o in options if o in names), None)
            pc_i = col("pcds", "postcode", "pcd", "pcd2")
            lat_i, lng_i = col("lat", "latitude"), col("long", "lng", "longitude")
            ctry_i, term_i = col("ctry", "ctry25cd", "country"), col("doterm")
            if None in (pc_i, lat_i, lng_i):
                raise ImportError_(f"{self.path.name}: needs postcode, lat and long columns.")
            for row in reader:
                pc = tidy_postcode(row[pc_i]) if pc_i < len(row) else None
                if pc not in postcodes:
                    continue
                if term_i is not None and row[term_i].strip():
                    continue                                 # terminated postcode
                try:
                    lat, lng = float(row[lat_i]), float(row[lng_i])
                except ValueError:
                    continue
                if lat > 90:                                 # ONSPD "no grid reference"
                    continue
                ctry = row[ctry_i].strip() if ctry_i is not None else ""
                out[pc] = {"lat": lat, "lng": lng,
                           "england": None if not ctry else ctry in ("E92000001", "England")}
        return out


class PostcodesIoLookup:
    """postcodes.io bulk lookup, 100 per request, cached on disk for re-runs."""

    def __init__(self, base_url: str = "https://api.postcodes.io", cache: Path | None = None,
                 client: httpx.Client | None = None, pause_s: float = 0.1):
        self.base_url = base_url.rstrip("/")
        self.cache_path = cache
        self.client = client or httpx.Client(timeout=30)
        self.pause_s = pause_s

    def _load(self) -> dict:
        if self.cache_path and self.cache_path.exists():
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        return {}

    def _save(self, cache: dict) -> None:
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(cache), encoding="utf-8")

    def lookup(self, postcodes: set[str]) -> dict[str, dict]:
        cache = self._load()
        todo = sorted(p for p in postcodes if p not in cache)
        for i in range(0, len(todo), 100):
            batch = todo[i:i + 100]
            for attempt in range(3):
                try:
                    r = self.client.post(f"{self.base_url}/postcodes", json={"postcodes": batch})
                    r.raise_for_status()
                    break
                except httpx.HTTPError:
                    if attempt == 2:
                        self._save(cache)
                        raise
                    time.sleep(2 ** attempt)
            for item in r.json().get("result") or []:
                res = item.get("result")
                pc = tidy_postcode(item.get("query") or "")
                if pc:
                    cache[pc] = None if not res else {
                        "lat": res["latitude"], "lng": res["longitude"],
                        "england": None if not res.get("country") else res["country"] == "England"}
            if (i // 100) % 20 == 19:
                self._save(cache)
                print(f"  postcodes: {min(i + 100, len(todo))}/{len(todo)}", file=sys.stderr)
            time.sleep(self.pause_s)
        self._save(cache)
        return {p: cache[p] for p in postcodes if cache.get(p)}


# ---------------------------------------------------------------- build
def build(pharmacy_files: list[Path], gp_files: list[Path], geo, *,
          england_only: bool = True, today: dt.date | None = None) -> tuple[dict, dict]:
    today = today or dt.date.today()
    skipped: dict[str, int] = {}
    kept: dict[str, dict] = {}
    sources, nhsbsa = [], False

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    staged = []
    for kind, files in (("pharmacy", pharmacy_files), ("gp_practice", gp_files)):
        for path in files:
            recs, info = records_from(path)
            sources.append({**info, "kind": kind})
            nhsbsa |= info["nhsbsa_like"]
            for r in recs:
                reason = (pharmacy_skip_reason if kind == "pharmacy" else gp_skip_reason)(r)
                if reason:
                    skip(reason)
                    continue
                code = (r.get("code") or "").upper()
                pc = tidy_postcode(r.get("postcode"))
                if not code or not r.get("name"):
                    skip("missing code or name")
                    continue
                if not pc:
                    skip("missing or invalid postcode")
                    continue
                staged.append((kind, code, pc, r))

    where = geo.lookup({pc for _, _, pc, _ in staged})
    for kind, code, pc, r in staged:
        g = where.get(pc)
        if not g:
            skip("postcode not found")
            continue
        if england_only and g.get("england") is False:
            skip("outside England")
            continue
        sid = ("nhs-pharm:" if kind == "pharmacy" else "nhs-gp:") + code
        if sid in kept:
            skip("duplicate code")
            continue
        kept[sid] = {
            "id": sid, "ods": code, "service_type": kind, "name": nice(r["name"]),
            "address": ", ".join(nice(a) for a in r.get("address_lines") or []) or None,
            "postcode": pc, "lat": round(g["lat"], 5), "lng": round(g["lng"], 5),
            "phone": (r.get("phone") or "").strip() or None,
            # Not in the registers: UNKNOWN, never guessed.
            "hours": None, "pharmacy_first": None, "walk_in": None,
            "bookable": False, "confidential": False, "min_age": 0,
            "source": "nhs_open_data",
        }
    statements = [OGL]
    if nhsbsa:
        statements.append(f"Pharmacy data: NHSBSA Copyright {today.year}.")
    statements.append("Organisation data: NHS England Organisation Data Service (ODS).")
    meta = {"generated": today.isoformat(), "attribution": " ".join(statements),
            "sources": sources,
            "counts": {k: sum(1 for s in kept.values() if s["service_type"] == k)
                       for k in ("pharmacy", "gp_practice")},
            "skipped": dict(sorted(skipped.items())),
            "england_only": england_only,
            "note": "Opening hours, Pharmacy First participation and walk-in are not in "
                    "these registers; they are null (unknown)."}
    return {"meta": meta, "services": sorted(kept.values(), key=lambda s: s["id"])}, skipped


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pharmacies", nargs="+", type=Path, default=[])
    ap.add_argument("--gp-practices", nargs="+", type=Path, default=[])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--postcodes", type=Path, help="ONS Postcode Directory (or similar) CSV")
    g.add_argument("--postcodes-io", action="store_true", help="look up on postcodes.io")
    ap.add_argument("--postcodes-io-url", default="https://api.postcodes.io")
    ap.add_argument("--all-uk", action="store_true", help="keep services outside England too")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args(argv)
    if not a.pharmacies and not a.gp_practices:
        ap.error("give --pharmacies and/or --gp-practices")
    geo = (OnspdLookup(a.postcodes) if a.postcodes else
           PostcodesIoLookup(a.postcodes_io_url, cache=DEFAULT_CACHE))
    try:
        doc, skipped = build(a.pharmacies, a.gp_practices, geo, england_only=not a.all_uk)
    except ImportError_ as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    c = doc["meta"]["counts"]
    print(f"wrote {a.out} ({a.out.stat().st_size / 1e6:.1f} MB): "
          f"{c['pharmacy']} pharmacies, {c['gp_practice']} GP practices")
    for reason, n in skipped.items():
        print(f"  skipped {n:>6}  {reason}")
    print(f"attribution: {doc['meta']['attribution']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
