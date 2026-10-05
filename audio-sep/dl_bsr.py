import os, time, sys
import requests

URL = "https://hf-mirror.com/enerjazzer/BS-ROFO-SW-Fixed/resolve/main/BS-Rofo-SW-Fixed.ckpt"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "bsr.ckpt")
TMP = OUT + ".part"

def main():
    for attempt in range(1, 31):
        try:
            start = os.path.getsize(TMP) if os.path.exists(TMP) else 0
            headers = {"Range": f"bytes={start}-"} if start else {}
            with requests.get(URL, headers=headers, stream=True, timeout=60) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length", 0)) + start
                mode = "ab" if start else "wb"
                with open(TMP, mode) as f:
                    done = start
                    for chunk in r.iter_content(1 << 20):
                        if not chunk:
                            continue
                        f.write(chunk)
                        done += len(chunk)
                        if done % (25 * 1024 * 1024) < (1 << 20) or done == total:
                            print(f"\r{done//1024//1024}/{total//1024//1024} MB  (att{attempt})",
                                  end="", flush=True)
                # 校验大小
                if os.path.getsize(TMP) == total:
                    os.replace(TMP, OUT)
                    print(f"\nDL_OK size={os.path.getsize(OUT)}")
                    return 0
                else:
                    print(f"\natt{attempt} size mismatch {os.path.getsize(TMP)}/{total}, retry")
        except Exception as e:
            print(f"\natt{attempt} err: {e}", flush=True)
            time.sleep(3)
    print("DL_GAVE_UP")
    return 1

if __name__ == "__main__":
    sys.exit(main())
