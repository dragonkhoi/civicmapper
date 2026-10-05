#!/usr/bin/env python3
"""
Prospect candidate jurisdictions for CivicMapper: search ArcGIS Online for public parcel
layers, probe every candidate layer live, score it, and print a ranked shortlist with the
disqualifying reason for every layer that fails.

Why: hunting one city at a time costs a session per dead end (Louisville: no values;
Alexandria/Falls Church/Loudoun/Prince William: values behind WAF'd lookup apps). The
structural sweet spots are jurisdictions whose parcel layer is already city-only, so no
clip is needed: Virginia independent cities and consolidated city-counties.

Criteria (each layer must pass all six to be "GO"):
  1. reachable   - layer metadata answers publicly, no token
  2. polygons    - geometryType is esriGeometryPolygon
  3. land+imp    - separate land-value AND improvement/building-value fields exist
  4. populated   - in a random-ish sample, >=70% of rows have land value > 0
  5. coverage    - feature count is 0.5x-2x the expected parcel count (layer is the whole
                   roll for this jurisdiction, not a slice or a whole-state layer)
  6. class       - a land-use / property-class field exists (needed for Vacant/Parking/
                   exempt buckets)

Usage:
    python data/scripts/prospect_cities.py                     # built-in VA + city-county list
    python data/scripts/prospect_cities.py --only hampton suffolk
    python data/scripts/prospect_cities.py --url hampton=https://.../FeatureServer/0
    python data/scripts/prospect_cities.py --json out.json

Hosts are often firewalled from cloud sandboxes (see add-city skill §1); run this from a
machine that can reach www.arcgis.com and services*.arcgis.com.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field

import requests

AGOL_SEARCH = "https://www.arcgis.com/sharing/rest/search"
TIMEOUT = 30

# key -> (search name, state, expected parcel count ~ order of magnitude)
CANDIDATES: dict[str, tuple[str, str, int]] = {
    # Virginia independent cities not yet in the app (Richmond, Lynchburg, Newport News,
    # Charlottesville, Fairfax City are shipped; Alexandria/Falls Church are blocked).
    "virginiabeach": ("Virginia Beach", "VA", 150_000),
    "norfolk": ("Norfolk", "VA", 70_000),
    "chesapeake": ("Chesapeake", "VA", 95_000),
    "hampton": ("Hampton", "VA", 50_000),
    "portsmouth": ("Portsmouth", "VA", 40_000),
    "suffolk": ("Suffolk", "VA", 45_000),
    "roanoke": ("Roanoke", "VA", 45_000),
    "danville": ("Danville", "VA", 25_000),
    "petersburg": ("Petersburg", "VA", 16_000),
    "winchester": ("Winchester", "VA", 10_000),
    "harrisonburg": ("Harrisonburg", "VA", 12_000),
    "salem": ("Salem", "VA", 11_000),
    "fredericksburg": ("Fredericksburg", "VA", 10_000),
    "staunton": ("Staunton", "VA", 12_000),
    "waynesboro": ("Waynesboro", "VA", 10_000),
    "williamsburg": ("Williamsburg", "VA", 4_000),
    "hopewell": ("Hopewell", "VA", 10_000),
    "bristol": ("Bristol", "VA", 10_000),
    "manassas": ("Manassas", "VA", 12_000),
    "colonialheights": ("Colonial Heights", "VA", 8_000),
    "martinsville": ("Martinsville", "VA", 9_000),
    # Consolidated city-counties (Kentucky's skipped: PVAs withhold values)
    "nashville": ("Davidson County", "TN", 280_000),
    "jacksonville": ("Duval County", "FL", 380_000),
    "indianapolis": ("Marion County", "IN", 350_000),
    "neworleans": ("Orleans Parish", "LA", 160_000),
    "anchorage": ("Anchorage", "AK", 100_000),
    "broomfield": ("Broomfield", "CO", 30_000),
    "augusta": ("Augusta Richmond County", "GA", 85_000),
    "columbusga": ("Columbus Muscogee County", "GA", 80_000),
    "athens": ("Athens-Clarke County", "GA", 45_000),
    "kansascityks": ("Wyandotte County", "KS", 70_000),
    "honolulu": ("Honolulu", "HI", 300_000),
    "sanfrancisco": ("San Francisco", "CA", 210_000),
}

LAND_RE = re.compile(r"(^|_)(land|lnd)(_?(val|value|appr|assess|asmt|mkt|av|amt)|$)|landval|landvalue|apprland|assdland|cur.*land|land.*cur", re.I)
IMP_RE = re.compile(r"(impr?|bldg|building|improvement|struct)(_?(val|value|appr|assess|asmt|mkt|av|amt))?|imp_?val|bldgval", re.I)
CLASS_RE = re.compile(r"land_?use|luc|use_?code|prop_?(clas|class|type|use)|clas|state_?cd|zoning_?use|usedesc|sptb|stclass|pcdesc", re.I)


@dataclass
class Probe:
    key: str
    title: str
    url: str
    checks: dict[str, bool] = field(default_factory=dict)
    count: int | None = None
    land_field: str | None = None
    imp_field: str | None = None
    class_field: str | None = None
    land_fill: float | None = None
    reason: str = ""

    @property
    def score(self) -> int:
        return sum(self.checks.values())

    @property
    def go(self) -> bool:
        return self.score == 6


def get(url: str, **params) -> dict:
    params.setdefault("f", "json")
    r = requests.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    if "error" in d:
        raise RuntimeError(d["error"].get("message", "arcgis error"))
    return d


def search_layers(name: str, state: str, limit: int = 15) -> list[tuple[str, str]] | None:
    """AGOL search -> [(title, layer url)], or None if the search itself failed.
    Expands service urls to their polygon parcel layers."""
    out: list[tuple[str, str]] = []
    q = f'{name} {state} parcels (type:"Feature Service" OR type:"Map Service")'
    try:
        res = get(AGOL_SEARCH, q=q, num=limit, sortField="numViews", sortOrder="desc")["results"]
    except Exception as e:  # noqa: BLE001
        print(f"  ! search failed: {e}", file=sys.stderr)
        return None
    for item in res:
        url = (item.get("url") or "").rstrip("/")
        if not url:
            continue
        if re.search(r"/(Feature|Map)Server/\d+$", url):
            out.append((item["title"], url))
            continue
        try:
            svc = get(url)
        except Exception:  # noqa: BLE001
            out.append((item["title"], url))  # let probe() record the failure
            continue
        for lyr in svc.get("layers", []):
            if lyr.get("geometryType") in (None, "esriGeometryPolygon") and re.search(r"parcel|tax|cadast|lot|property", lyr.get("name", ""), re.I):
                out.append((f'{item["title"]} / {lyr["name"]}', f'{url}/{lyr["id"]}'))
    return list(dict.fromkeys(out))


def pick(fields: list[str], rx: re.Pattern, exclude: set[str] = frozenset()) -> str | None:
    hits = [f for f in fields if rx.search(f) and f not in exclude]
    # prefer the most "value-like" name
    hits.sort(key=lambda f: (not re.search(r"val|appr|assess|asmt|mkt|av$", f, re.I), len(f)))
    return hits[0] if hits else None


def probe(key: str, title: str, url: str, expected: int) -> Probe:
    p = Probe(key, title, url)
    try:
        meta = get(url)
    except Exception as e:  # noqa: BLE001
        p.checks["reachable"] = False
        p.reason = f"unreachable: {e}"[:160]
        return p
    p.checks["reachable"] = True
    p.checks["polygons"] = meta.get("geometryType") == "esriGeometryPolygon"

    fields = [f["name"] for f in meta.get("fields", [])]
    numeric = [f["name"] for f in meta.get("fields", []) if f.get("type") in ("esriFieldTypeDouble", "esriFieldTypeInteger", "esriFieldTypeSingle", "esriFieldTypeSmallInteger", "esriFieldTypeBigInteger")]
    p.land_field = pick(numeric, LAND_RE)
    p.imp_field = pick(numeric, IMP_RE, {p.land_field} if p.land_field else set())
    p.class_field = pick(fields, CLASS_RE)
    p.checks["land+imp"] = bool(p.land_field and p.imp_field)
    p.checks["class"] = bool(p.class_field)

    try:
        p.count = get(f"{url}/query", where="1=1", returnCountOnly="true")["count"]
    except Exception:  # noqa: BLE001
        p.count = None
    p.checks["coverage"] = p.count is not None and 0.5 * expected <= p.count <= 2 * expected

    p.checks["populated"] = False
    if p.land_field:
        try:
            # pull from the middle of the table, not just the first OIDs (often stubs)
            feats = get(f"{url}/query", where="1=1", outFields=p.land_field, returnGeometry="false",
                        resultOffset=max(0, (p.count or 0) // 2), resultRecordCount=500)["features"]
            vals = [f["attributes"].get(p.land_field) for f in feats]
            if vals:
                p.land_fill = sum(1 for v in vals if v and v > 0) / len(vals)
                p.checks["populated"] = p.land_fill >= 0.7
        except Exception:  # noqa: BLE001
            pass

    fails = [k for k, ok in p.checks.items() if not ok]
    detail = {
        "polygons": f"geometry={meta.get('geometryType')}",
        "land+imp": f"land={p.land_field} imp={p.imp_field}",
        "populated": f"land>0 in {p.land_fill:.0%} of sample" if p.land_fill is not None else "no sample",
        "coverage": f"count={p.count} vs expected~{expected}",
        "class": "no land-use/class field",
    }
    p.reason = "; ".join(f"{k}: {detail.get(k, '')}" for k in fails)
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", help="candidate keys to run")
    ap.add_argument("--url", action="append", default=[], help="key=layer_url: probe a known layer directly")
    ap.add_argument("--json", help="write all probe results here")
    args = ap.parse_args()

    direct: dict[str, list[str]] = {}
    for kv in args.url:
        k, _, u = kv.partition("=")
        direct.setdefault(k, []).append(u)

    keys = args.only or (list(direct) if direct else list(CANDIDATES))
    results: list[Probe] = []
    for key in keys:
        name, state, expected = CANDIDATES.get(key, (key, "", 0))
        print(f"== {key} ({name}, {state})", file=sys.stderr)
        layers = [(u, u) for u in direct.get(key, [])] or search_layers(name, state)
        if layers is None:
            results.append(Probe(key, "-", "-", {"reachable": False}, reason="AGOL search unreachable"))
            continue
        if not layers:
            results.append(Probe(key, "-", "-", {"reachable": False}, reason="no AGOL parcel layer found"))
            continue
        for title, url in layers:
            results.append(probe(key, title, url, expected))

    # best layer per jurisdiction, then rank jurisdictions
    best: dict[str, Probe] = {}
    for p in results:
        if p.key not in best or (p.score, p.land_fill or 0) > (best[p.key].score, best[p.key].land_fill or 0):
            best[p.key] = p
    ranked = sorted(best.values(), key=lambda p: (-p.score, -(p.land_fill or 0)))

    print(f"\n{'key':<16}{'score':<7}{'count':>9}  verdict / reason")
    for p in ranked:
        verdict = "GO  " + f"{p.land_field}/{p.imp_field}/{p.class_field}  {p.url}" if p.go else p.reason
        print(f"{p.key:<16}{p.score}/6    {p.count if p.count is not None else '-':>9}  {verdict}")

    if args.json:
        with open(args.json, "w") as f:
            json.dump([asdict(p) | {"score": p.score} for p in results], f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
