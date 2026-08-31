#!/usr/bin/env bash
# The whole pipeline, end to end.
#
#   ./run_all.sh "<file.pbix | file.pbip | PBIP folder>"
#
# Stages, each usable on its own:
#   1 extract    binary model      -> parquet + duckdb + model.json
#   2 report     report definition -> report-spec.json
#   3 replicate  spec + data       -> build/replica/index.html
#   4 analyse    model             -> build/analysis/findings.{json,md}
#   5 strategic  findings + read   -> build/strategic/index.html
set -euo pipefail
cd "$(dirname "$0")"

if [ $# -lt 1 ]; then
  echo "usage: ./run_all.sh <file.pbix | file.pbip | PBIP folder>" >&2
  echo "no sample ships with this repo; see the README for where to get one." >&2
  exit 2
fi
SRC="$1"
if [ ! -e "$SRC" ]; then
  echo "not found: $SRC" >&2
  exit 2
fi
echo "== 1/5 extract          $SRC"
python3 tools/pbi_extract.py "$SRC" -o build/model

echo "== 2/5 report definition"
python3 tools/pbir_parse.py "$SRC" -o build/model/report-spec.json

echo "== 3/5 replica"
python3 tools/replicate_html.py --model build/model --out build/replica

echo "== 4/5 analysis"
python3 tools/analyze_model.py --model build/model -o build/analysis

echo "== 5/5 strategic dashboard"
if [ ! -f build/analysis/narrative.json ]; then
  echo "   no narrative.json yet - the page will show evidence with no read on it."
  echo "   Have an agent read build/analysis/findings.md and write one (see skills/pbi-redesign)."
fi
python3 tools/strategic_html.py --model build/model \
  --findings build/analysis/findings.json \
  --narrative build/analysis/narrative.json \
  --out build/strategic

echo
echo "open build/replica/index.html    the original report, rebuilt"
echo "open build/strategic/index.html  the executive review"
echo "then: python3 tools/pbi_chat.py schema"
