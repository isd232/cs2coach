import os, sys, gzip, shutil, argparse, time
from pathlib import Path
import requests

API = "https://open.faceit.com/data/v4"
DL = "https://open.faceit.com/download/v2/demos/download"
OUT = Path("demos")


def hdr():
    key = os.environ.get("FACEIT_API_KEY")
    if not key:
        sys.exit("Ustaw zmienna srodowiskowa FACEIT_API_KEY.")
    return {"Authorization": f"Bearer {key}"}


def get(path, **params):
    r = requests.get(f"{API}{path}", headers=hdr(), params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def unpack(f: Path) -> Path:
    if f.suffix == ".gz":
        dst = f.with_suffix("")
        with gzip.open(f, "rb") as a, open(dst, "wb") as b:
            shutil.copyfileobj(a, b)
    elif f.suffix == ".zst":
        import zstandard
        dst = f.with_suffix("")
        with open(f, "rb") as a, open(dst, "wb") as b:
            zstandard.ZstdDecompressor().copy_stream(a, b)
    else:
        return f
    f.unlink()
    return dst


def unpack_all():
    for f in list(OUT.glob("*.gz")) + list(OUT.glob("*.zst")):
        print("Rozpakowuje", f.name)
        unpack(f)


def signed_url(resource_url: str) -> str:
    r = requests.post(DL, headers=hdr(), json={"resource_url": resource_url}, timeout=30)
    if r.status_code in (401, 403):
        sys.exit("Downloads API acces denied.")
    r.raise_for_status()
    return r.json()["payload"]["download_url"]


def download(url: str, dst: Path):
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dst, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("nick", nargs="?")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--unpack-only", action="store_true")
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    if a.unpack_only:
        return unpack_all()
    if not a.nick:
        sys.exit("Podaj nick FACEIT.")

    pid = get("/players", nickname=a.nick)["player_id"]
    items = get(f"/players/{pid}/history", game="cs2", limit=min(a.limit, 100))["items"]
    print(f"Znaleziono {len(items)} meczow")

    for it in items:
        mid = it["match_id"]
        if any(OUT.glob(f"{mid}*")):
            print("Juz jest:", mid); continue
        urls = get(f"/matches/{mid}").get("demo_url") or []
        if not urls:
            print("Brak dema (mecz starszy niz ok. 30 dni?):", mid); continue
        res = urls[0]
        name = res.split("/")[-1].split("?")[0]
        dst = OUT / f"{mid}__{name}"
        try:
            print("Pobieram", mid)
            download(signed_url(res), dst)
            unpack(dst)
        except Exception as e:
            print("  blad:", e)
        time.sleep(1)


if __name__ == "__main__":
    main()
