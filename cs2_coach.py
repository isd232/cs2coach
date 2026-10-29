import sys, json, hashlib
from pathlib import Path
from collections import Counter

# config
STEAM_ID   = !!!provide steam id before use
DEMOS_DIR  = Path("demos")
CACHE_DIR  = Path("cache")
REPORT     = Path("raport.md")
OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL      = "qwen3:8b"
TICKRATE   = 64
TRADE_SEC  = 5
EARLY_SEC  = 20
GRID       = 250


def col(df, *names):
    for n in names:
        if n in df.columns:
            return n
    raise KeyError(f"Brak kolumny z {names}. Dostepne: {df.columns}")


def analyze_demo(path: Path) -> dict:
    from awpy import Demo
    demo = Demo(str(path))
    demo.load("rounds", "kills", "damages")

    rd = {}
    for r in demo.rounds.to_dicts():
        if r.get("is_knife_round"):
            continue
        r["end_t"] = r.get("official_end_tick") or r.get("end_tick")
        if r.get("start_tick") is None or r["end_t"] is None:
            continue
        if r.get("freeze_end_tick") is None:
            r["freeze_end_tick"] = r["start_tick"]
        rd[r["round_num"]] = r
    if not rd:
        raise ValueError("brak rund w demie")

    def rnd_of(tick):
        if tick is None:
            return None
        for n, r in rd.items():
            if r["start_tick"] <= tick <= r["end_t"]:
                return n
        return None

    kl = []
    for k in demo.kills.to_dicts():
        rn = rnd_of(k["tick"])
        if rn is not None: #exclude warmup nd knife
            k["rn"] = rn
            kl.append(k)

    mine_deaths = [k for k in kl if k["victim_steamid"] == STEAM_ID]
    mine_kills = [k for k in kl if k["attacker_steamid"] == STEAM_ID
                  and k["attacker_side"] != k["victim_side"]]

    traded = early = 0
    spots, details = Counter(), []
    for d in mine_deaths:
        t_since = (d["tick"] - rd[d["rn"]]["freeze_end_tick"]) / TICKRATE
        got_trade = bool(d["victim_traded"])
        is_early = t_since < EARLY_SEC
        traded += got_trade
        early += is_early
        spots[(round(d["victim_x"] / GRID) * GRID, round(d["victim_y"] / GRID) * GRID)] += 1
        if not got_trade:
            details.append({"round": d["rn"], "sec": round(t_since), "side": d["victim_side"],
                            "weapon_of_killer": d["weapon"], "early": bool(is_early)})

    total_dmg = 0
    for r in demo.damages.to_dicts():
        if (r["attacker_steamid"] == STEAM_ID and r["attacker_side"] != r["victim_side"]
                and rnd_of(r["tick"]) is not None):
            total_dmg += r["health_pre"] - r["health_post"]
    adr = round(total_dmg / len(rd), 1)

    return {
        "demo": path.name,
        "map": demo.header.get("map_name"),
        "rounds": len(rd),
        "kills": len(mine_kills),
        "deaths": len(mine_deaths),
        "adr": adr,
        "deaths_traded": int(traded),
        "deaths_untraded": len(mine_deaths) - int(traded),
        "early_deaths": int(early),
        "deaths_by_side": dict(Counter(d["victim_side"] for d in mine_deaths)),
        "top_death_spots": [{"xy": list(s), "n": n} for s, n in spots.most_common(3)],
        "untraded_death_examples": details[:8],
    }


def cached(path: Path) -> dict:
    CACHE_DIR.mkdir(exist_ok=True)
    key = hashlib.md5(f"{path.name}{path.stat().st_size}{STEAM_ID}".encode()).hexdigest()
    f = CACHE_DIR / f"{key}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    res = analyze_demo(path)
    f.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    return res


def aggregate(results: list[dict]) -> dict:
    n = len(results)
    tot = lambda k: sum(r[k] for r in results)
    deaths = max(tot("deaths"), 1)
    per_map = {}
    for r in results:
        m = per_map.setdefault(r["map"], {"demos": 0, "kills": 0, "deaths": 0, "untraded": 0, "early": 0, "spots": Counter()})
        m["demos"] += 1; m["kills"] += r["kills"]; m["deaths"] += r["deaths"]
        m["untraded"] += r["deaths_untraded"]; m["early"] += r["early_deaths"]
        for s in r["top_death_spots"]:
            m["spots"][tuple(s["xy"])] += s["n"]
    for m in per_map.values():
        m["spots"] = [{"xy": list(k), "n": v} for k, v in m["spots"].most_common(3)]
    adrs = [r["adr"] for r in results if r["adr"] is not None]
    return {
        "demos_analyzed": n,
        "total_rounds": tot("rounds"),
        "kd": round(tot("kills") / deaths, 2),
        "avg_adr": round(sum(adrs) / len(adrs), 1) if adrs else None,
        "pct_deaths_untraded": round(100 * tot("deaths_untraded") / deaths),
        "pct_deaths_early": round(100 * tot("early_deaths") / deaths),
        "per_map": per_map,
        "per_demo_trend": [{"demo": r["demo"], "adr": r["adr"], "kd": round(r["kills"] / max(r["deaths"], 1), 2)} for r in results],
    }


PROMPT = """Jestes doswiadczonym trenerem CS2. Ponizej masz zagregowane statystyki gracza z {n} meczow
(JSON). Odpowiadaj PO POLSKU. Zasady:
- Uzywaj WYLACZNIE liczb z JSON-a, nie wymyslaj danych.
- Podaj 3 najwazniejsze powtarzalne bledy (kazdy z liczba jako dowodem).
- Do kazdego bledu dodaj jedno konkretne cwiczenie na 15 minut.
- Wskaz 1 mocna strone.
- Jesli probka jest mala (<5 meczow), zaznacz, ze wnioski sa wstepne.
- Wspolrzedne xy to kratki mapy; nie zgaduj nazw miejsc, napisz tylko, ze smierci skupiaja sie w danym obszarze.

DANE:
{data}
"""


def ask_ollama(prompt: str) -> str:
    import requests
    r = requests.post(OLLAMA_URL, json={
        "model": MODEL, "stream": False,
        "messages": [{"role": "user", "content": prompt}],
        "options": {"temperature": 0.3},
    }, timeout=600)
    r.raise_for_status()
    return r.json()["message"]["content"]


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "inspect":
        from awpy import Demo
        d = Demo(sys.argv[2]); d.load("rounds", "kills", "damages")
        for name in ("rounds", "kills", "damages"):
            print(name, getattr(d, name).columns)
        print(d.header)
        return
    demos = sorted(DEMOS_DIR.glob("*.dem"))
    if not demos:
        sys.exit(f"Brak plikow .dem w {DEMOS_DIR}/")
    results = []
    for p in demos:
        try:
            print("Analiza:", p.name)
            results.append(cached(p))
        except Exception as e:
            print("  pominieto:", e)
    if not results:
        sys.exit("Zadne demo nie zostalo przeanalizowane.")
    agg = aggregate(results)
    data = json.dumps(agg, ensure_ascii=False, indent=1)
    report = ask_ollama(PROMPT.format(n=agg["demos_analyzed"], data=data))
    REPORT.write_text(f"# Raport CS2\n\n{report}\n\n---\n<details><summary>Dane</summary>\n\n```json\n{data}\n```\n</details>\n", encoding="utf-8")
    print("Zapisano", REPORT)


if __name__ == "__main__":
    main()
