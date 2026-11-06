"""Temporary probe #2: why do two Oct 22 Viebit recordings show no captions,
and when did Viebit create recent files? Read-only; prints only."""
import json, re, requests
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}
from datetime import datetime, timezone

for url in ["https://councilnyc.viebit.com/vod/?s=true&v=NYCC-PV-CH-COM_261022-Cultural.mp4",
            "https://councilnyc.viebit.com/vod/?s=true&v=NYCC-PV-CH-COM_261022-Rules.mp4",
            "https://councilnyc.viebit.com/vod/?s=true&v=NYCC-PV-CH-CHA_261027-132216.mp4"]:
    r = requests.get(url, headers=UA, timeout=30)
    print("\n###", url, "->", r.status_code, r.url, len(r.text))
    for m in re.finditer(r"vtt|track|caption|subtitle|\.srt|\.m3u8|\"src\"", r.text, re.I):
        s = r.text[max(0, m.start()-120): m.end()+160].replace("\n", " ")
        print("  ..", s)
    print("  ---- distinct src values:", sorted(set(re.findall(r'"src"\s*:\s*"([^"]+)"', r.text)))[:10])

base = "https://councilnyc.viebit.com/vb/public/vod"
def show(params):
    try:
        r = requests.get(base, params=params, headers=UA, timeout=30)
        j = r.json()
        res = j.get("result") or []
        keys = list(j.keys())
        print(f"\n{params} -> {r.status_code} keys={keys} filtered={j.get('filtered')} n={len(res)}")
        for it in res[:6]:
            cd = it.get("created_date")
            print("   ", it.get("filename"), it.get("id"),
                  datetime.fromtimestamp(cd, timezone.utc).isoformat() if cd else None)
    except Exception as ex:
        print(params, "FAILED", repr(ex))

show({})
print(json.dumps(requests.get(base, headers=UA, timeout=30).json()["result"][0], indent=1)[:1500])
for p in [{"search": "261027"}, {"q": "261027"}, {"filter": "261027"}, {"keyword": "261027"},
          {"sort": "created_date", "order": "desc"}, {"order": "desc"}, {"sort": "-created_date"},
          {"orderby": "created_date", "dir": "desc"}, {"start": 5380}, {"offset": 5380}, {"page": 538},
          {"length": 5, "start": 5384}, {"search[value]": "261027"}, {"title": "261027"}]:
    show(p)
