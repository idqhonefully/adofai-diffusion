import sys, json, urllib.request
sys.path.insert(0, r'<REPO>\gui')
import backend
backend.start()
midi = r'<REPO>\output\.work\FallenEra_1789790176\FallenEra_stems_combined.mid'

def post(path, body):
    req = urllib.request.Request(
        "http://127.0.0.1:8765" + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=180).read())

r = post("/api/load", {"path": midi})
print("load ok:", r.get("ok"), "name:", r.get("name"))
state = {
    "tracks_checked": r.get("default_tracks_checked", []),
    "sub_checked": r.get("default_sub_checked", []),
    "dp_checked": r.get("default_dp_checked", []),
    "current_track": r.get("default_current_track", 0),
}
r = post("/api/derive", {"state": state})
print("derive ok:", r.get("ok"))
r = post("/api/rebuild", {"state": state})
print("rebuild ok:", r.get("ok"))
print("ROOT n_floors:", r.get("n_floors"))
print("PAYLOAD n_floors:", (r.get("payload") or {}).get("n_floors"))
print("payload keys sample:", sorted((r.get("payload") or {}).keys())[:12])
