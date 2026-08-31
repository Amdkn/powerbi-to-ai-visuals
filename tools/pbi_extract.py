#!/usr/bin/env python3
"""Stage 1 of the pipeline: a Power BI file in, an open data model out.

    python3 tools/pbi_extract.py "<file.pbix | file.pbip | PBIP folder>" -o build/model

Produces, under the output folder:

    tables/<Table>.parquet   one file per user table (auto date tables dropped)
    model.duckdb             every table loaded, ready to query
    model.json               the semantic model: columns, relationships,
                             measures (with DAX), hierarchies, sort-by columns,
                             partition M queries, row counts

Why both a .pbix and a .pbip path work: the .pbix stores the VertiPaq database
as a ``DataModel`` part, and a saved .pbip keeps the same bytes in
``*.SemanticModel/.pbi/cache.abf``. Both are XPress9-compressed Analysis
Services backups, so the same reader handles them. The .pbip additionally ships
TMDL, which carries names, hierarchies and DAX that the binary alone does not
describe as cleanly, so when both are present TMDL wins for metadata and the
binary is used for data.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pbi_common import (PROJECT_ROOT, is_auto_date_table, log, require,
                        write_json)

require("pandas", "duckdb", "pyarrow", "pbixray")

import duckdb                     # noqa: E402
import pandas as pd               # noqa: E402
import tmdl                       # noqa: E402
from pbixray import PBIXRay       # noqa: E402


# ------------------------------------------------------------------ discovery

def resolve_source(path: Path) -> dict:
    """Work out where the data bytes and the TMDL/PBIR definitions live."""
    path = path.resolve()
    src = {"input": str(path), "pbix": None, "semantic_dir": None, "report_dir": None,
           "abf": None, "kind": None}

    if path.is_file() and path.suffix.lower() == ".pbix":
        src["kind"] = "pbix"
        src["pbix"] = path
        # A PBIP folder sitting next to the pbix is a bonus, not a requirement.
        sibling = _find_pbip_root(path.parent) or _find_pbip_root(path.parent.parent)
        if sibling:
            src.update(_pbip_parts(sibling))
        return src

    root = path
    if path.is_file() and path.suffix.lower() == ".pbip":
        root = path.parent
    if root.is_dir():
        found = _find_pbip_root(root)
        if not found:
            raise SystemExit(f"No .SemanticModel / .Report folders found under {root}")
        src["kind"] = "pbip"
        src.update(_pbip_parts(found))
        if src["semantic_dir"]:
            abf = src["semantic_dir"] / ".pbi" / "cache.abf"
            if abf.exists():
                src["abf"] = abf
        near = sorted(root.parent.rglob("*.pbix"))
        if not src["abf"] and near:
            src["pbix"] = near[0]
        return src

    raise SystemExit(f"Not a .pbix, .pbip or PBIP folder: {path}")


def _find_pbip_root(start: Path) -> Path | None:
    for cand in [start, *start.iterdir()] if start.is_dir() else []:
        if not cand.is_dir():
            continue
        if list(cand.glob("*.SemanticModel")) or list(cand.glob("*.Report")):
            return cand
    return None


def _pbip_parts(root: Path) -> dict:
    sem = next(iter(root.glob("*.SemanticModel")), None)
    rep = next(iter(root.glob("*.Report")), None)
    return {"semantic_dir": sem, "report_dir": rep}


# ------------------------------------------------------------------ data read

def open_model(src: dict) -> tuple[PBIXRay, Path | None]:
    """PBIXRay wants a zip container; a bare cache.abf gets wrapped in one."""
    if src["pbix"]:
        return PBIXRay(str(src["pbix"])), None
    if src["abf"]:
        tmp = Path(tempfile.mkdtemp(prefix="pbip_abf_")) / "shim.pbix"
        with zipfile.ZipFile(tmp, "w") as z:
            z.write(src["abf"], "DataModel")
            z.writestr("Version", "1.28")
        log(f"  wrapping {src['abf'].name} for reading")
        return PBIXRay(str(tmp)), tmp.parent
    raise SystemExit(
        "No data found. A .pbix, or a .pbip whose SemanticModel keeps "
        ".pbi/cache.abf, is required. (Large models are sometimes saved "
        "without the cache; re-save the report in Power BI Desktop.)")


# ------------------------------------------------------------------- metadata

def model_from_tmdl(semantic_dir: Path) -> dict:
    defs = semantic_dir / "definition"
    tables, measures = [], []
    for f in sorted((defs / "tables").glob("*.tmdl")):
        t = tmdl.load_table(f)
        if not t or is_auto_date_table(t["name"]):
            continue
        measures.extend(t.pop("measures"))
        tables.append(t)
    rels = [r for r in tmdl.load_relationships(defs / "relationships.tmdl")
            if not is_auto_date_table(r["fromTable"]) and not is_auto_date_table(r["toTable"])]
    return {"source": "tmdl", "tables": tables, "measures": measures, "relationships": rels}


def model_from_binary(pbix: PBIXRay) -> dict:
    """Fallback for a .pbix with no PBIP alongside it."""
    schema = pbix.schema
    tables = {}
    for _, row in schema.iterrows():
        name = row["TableName"]
        if is_auto_date_table(name):
            continue
        tables.setdefault(name, {"name": name, "isHidden": False, "columns": [],
                                 "hierarchies": [], "partitions": []})
        tables[name]["columns"].append({
            "name": row["ColumnName"], "dataType": str(row["PandasDataType"]),
            "isHidden": False, "formatString": None, "summarizeBy": "none",
            "sourceColumn": row["ColumnName"], "sortByColumn": None,
            "expression": None, "isCalculated": False, "description": None})
    measures = []
    dm = pbix.dax_measures
    if dm is not None and len(dm):
        for _, r in dm.iterrows():
            measures.append({"name": r["Name"], "table": r["TableName"],
                             "expression": str(r["Expression"]).strip(),
                             "formatString": r.get("FormatString"),
                             "displayFolder": r.get("DisplayFolder"),
                             "description": r.get("Description"), "isHidden": False})
    rels = []
    rl = pbix.relationships
    if rl is not None and len(rl):
        for _, r in rl.iterrows():
            if is_auto_date_table(r["FromTableName"]) or is_auto_date_table(r["ToTableName"]):
                continue
            rels.append({"name": str(r.get("RelationshipId", "")),
                         "fromTable": r["FromTableName"], "fromColumn": r["FromColumnName"],
                         "toTable": r["ToTableName"], "toColumn": r["ToColumnName"],
                         "isActive": bool(r.get("IsActive", True)),
                         "crossFilteringBehavior": "singleDirection",
                         "fromCardinality": "many", "toCardinality": "one"})
    return {"source": "binary", "tables": list(tables.values()),
            "measures": measures, "relationships": rels}


# ----------------------------------------------------------------------- main

def extract(source: Path, out: Path, keep_auto_tables: bool = False) -> dict:
    src = resolve_source(source)
    log(f"source: {src['kind']}  ({src['input']})")
    pbix, tmpdir = open_model(src)

    out.mkdir(parents=True, exist_ok=True)
    tdir = out / "tables"
    tdir.mkdir(exist_ok=True)

    names = [t for t in pbix.tables if keep_auto_tables or not is_auto_date_table(t)]
    rows = {}
    for name in names:
        df = pbix.get_table(name)
        df = _normalize(df)
        df.to_parquet(tdir / f"{name}.parquet", index=False)
        rows[name] = len(df)
        log(f"  {name:<20} {len(df):>9,} rows  x {len(df.columns)} cols")

    if src["semantic_dir"] and (src["semantic_dir"] / "definition" / "tables").is_dir():
        model = model_from_tmdl(src["semantic_dir"])
        log("  metadata: TMDL")
    else:
        model = model_from_binary(pbix)
        log("  metadata: model binary (no PBIP alongside; hierarchies unavailable)")

    # Reconcile metadata against what actually materialised.
    model["tables"] = [t for t in model["tables"] if t["name"] in rows]
    for t in model["tables"]:
        t["rowCount"] = rows.get(t["name"], 0)
        t["parquet"] = f"tables/{t['name']}.parquet"
    model["measures"] = [m for m in model["measures"]
                         if not is_auto_date_table(m.get("table", ""))]
    model["relationships"] = [r for r in model["relationships"]
                              if r["fromTable"] in rows and r["toTable"] in rows]
    if src["semantic_dir"]:
        model["name"] = src["semantic_dir"].name.replace(".SemanticModel", "")
    elif src["pbix"]:
        model["name"] = src["pbix"].stem
    else:
        model["name"] = Path(src["input"]).stem
    model["sourceFile"] = src["input"]
    model["reportDir"] = str(src["report_dir"]) if src["report_dir"] else None
    model["totalRows"] = sum(rows.values())

    _infer_column_types(model, tdir)
    _load_duckdb(out / "model.duckdb", tdir, model)
    write_json(out / "model.json", model)

    if tmpdir:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return model


def _normalize(df: "pd.DataFrame") -> "pd.DataFrame":
    """pandas nullable extension dtypes do not round-trip cleanly to parquet
    for every reader; settle on plain numpy/object types."""
    for c in df.columns:
        s = df[c]
        if str(s.dtype) in ("Int64", "Int32", "Int16", "Int8"):
            df[c] = s.astype("float64") if s.isna().any() else s.astype("int64")
        elif str(s.dtype) in ("Float64", "Float32"):
            df[c] = s.astype("float64")
        elif str(s.dtype) == "boolean":
            df[c] = s.astype("object")
        elif str(s.dtype) == "string":
            df[c] = s.astype("object")
    return df


def _infer_column_types(model: dict, tdir: Path) -> None:
    for t in model["tables"]:
        f = tdir / f"{t['name']}.parquet"
        if not f.exists():
            continue
        head = pd.read_parquet(f).head(200)
        actual = {c: str(head[c].dtype) for c in head.columns}
        known = {c["name"] for c in t["columns"]}
        for c in t["columns"]:
            c["physicalType"] = actual.get(c["name"])
            if not c.get("dataType"):
                c["dataType"] = _dax_type(actual.get(c["name"]))
        # Calculated columns exist in the binary but not always in TMDL order.
        for cname, dtype in actual.items():
            if cname not in known:
                t["columns"].append({
                    "name": cname, "dataType": _dax_type(dtype), "isHidden": False,
                    "formatString": None, "summarizeBy": "none", "sourceColumn": cname,
                    "sortByColumn": None, "expression": None, "isCalculated": False,
                    "description": None, "physicalType": dtype})


def _dax_type(dtype: str | None) -> str:
    d = (dtype or "").lower()
    if "int" in d:
        return "int64"
    if "float" in d or "double" in d:
        return "double"
    if "datetime" in d or "date" in d:
        return "dateTime"
    if "bool" in d:
        return "boolean"
    return "string"


def _load_duckdb(db_path: Path, tdir: Path, model: dict) -> None:
    if db_path.exists():
        db_path.unlink()
    con = duckdb.connect(str(db_path))
    for t in model["tables"]:
        f = (tdir / f"{t['name']}.parquet").as_posix()
        con.execute(f'CREATE TABLE "{t["name"]}" AS SELECT * FROM read_parquet(\'{f}\')')
    con.close()
    log(f"  duckdb: {db_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help=".pbix, .pbip, or a folder containing a PBIP")
    ap.add_argument("-o", "--out", default=str(PROJECT_ROOT / "build" / "model"))
    ap.add_argument("--keep-auto-tables", action="store_true",
                    help="keep Power BI's hidden auto date/time tables")
    a = ap.parse_args()
    m = extract(Path(a.source), Path(a.out), a.keep_auto_tables)
    log(f"\n{len(m['tables'])} tables, {m['totalRows']:,} rows, "
        f"{len(m['measures'])} measures, {len(m['relationships'])} relationships "
        f"-> {a.out}")


if __name__ == "__main__":
    main()
