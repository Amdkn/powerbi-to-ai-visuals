#!/usr/bin/env bash
# Installs the one dependency that is not usually already present.
#
# pandas, duckdb and pyarrow are expected on the system Python. pbixray and its
# native codecs (xpress9, xpress8, xmhuffman, apsw, kaitaistruct) are vendored
# into ./vendor so nothing is installed system-wide and the repo stays portable.
set -euo pipefail
cd "$(dirname "$0")"

python3 - <<'PY'
import importlib, sys
missing = [m for m in ("pandas", "duckdb", "pyarrow")
           if not importlib.util.find_spec(m)]
if missing:
    sys.exit("Install these first (they are shared, not vendored): " + ", ".join(missing)
             + "\n  pip install " + " ".join(missing))
print("system deps ok: pandas, duckdb, pyarrow")
PY

echo "vendoring pbixray + native codecs into ./vendor"
pip3 install --quiet --target vendor --no-deps \
  pbixray kaitaistruct xpress9 xpress8 xmhuffman apsw

PYTHONPATH=vendor python3 -c "import pbixray; print('pbixray ready')"
echo
echo "next:  ./run_all.sh \"../Original PBIX/AdventureWorks Sales.pbix\""
