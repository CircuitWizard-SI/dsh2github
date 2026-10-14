"""Keep the npm plugin and Python wheel's embedded Host identical."""
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
target = root / "src/dsh2github/host"
target.mkdir(parents=True, exist_ok=True)
different = []
for source in (root / "harness-plugin").iterdir():
    if not source.is_file():
        continue
    destination = target / source.name
    if not destination.exists() or destination.read_bytes() != source.read_bytes():
        different.append(source.name)
        if "--check" not in sys.argv:
            destination.write_bytes(source.read_bytes())
if "--check" in sys.argv and different:
    raise SystemExit("Embedded Host is out of sync: " + ", ".join(different))
print("Host assets are synchronized")
