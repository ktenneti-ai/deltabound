"""Write viewer/test/fixtures/*.json from the sample estate via deltabound.bridge.

Run: .venv/bin/python viewer/scripts/gen_fixtures.py   (tests/test_bridge.py checks they are current)
"""
import json
from pathlib import Path

from deltabound import bridge

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "viewer" / "test" / "fixtures"
DATAFLOWS = ["DF_FACT_POS_SALES", "DF_FACT_LOYALTY_POINTS"]


def fixtures() -> dict[str, str]:
    files = [(p.name, p.read_bytes()) for p in sorted((ROOT / "demo" / "sap_bods" / "exports").glob("*.xml"))]
    payload = bridge.load(files)
    out = {}
    for df in DATAFLOWS:
        doc = {"dataflow": payload["estate"]["objects"][df], "lineage": bridge.lineage(df)}
        out[f"{df}.json"] = json.dumps(doc, indent=1, sort_keys=True) + "\n"
    return out


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in fixtures().items():
        (OUT / name).write_text(text)
        print(f"wrote {OUT / name}")
