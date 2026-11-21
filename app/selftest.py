"""Post-install hardware & module self-test.  Usage: python -m app.selftest [--quick]

Every check reports PASS / FAIL / SKIP honestly - nothing is assumed to work.
Exit code is 1 if any mandatory check FAILs.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import audio, config, converter, db, hardware, media, profiles, system

RESULTS: list[tuple[str, str, str]] = []


def rec(name: str, status: str, detail: str = "") -> None:
    RESULTS.append((name, status, detail))
    icon = {"PASS": "✔", "FAIL": "✘", "SKIP": "–", "WARN": "!"}[status]
    print(f" {icon} {status:4} {name:38} {detail}")


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def main() -> int:
    config.ensure_dirs()
    db.init()
    m = hardware.model()
    print(f"\nAV Player self-test – {hardware.model_name()} [{m}]  {time.ctime()}\n")
    rec("Raspberry Pi modeli", "PASS" if m in ("pi4", "pi5") else "WARN", f"{hardware.model_name()} → profil: {m}")
    rec("Veri klasörü yazılabilir", "PASS" if os.access(config.DATA_DIR, os.W_OK) else "FAIL", str(config.DATA_DIR))
    for c in ("ffmpeg", "ffprobe", "mpv", "aplay"):
        rec(f"komut: {c}", "PASS" if have(c) else "FAIL", shutil.which(c) or "kurulu değil")
    if not (have("ffmpeg") and have("ffprobe")):
        return finish()

    caps = hardware.probe_caps(force=True)
    for n, e in caps["encoders"].items():
        mandatory = n == "libx264"
        rec(f"encoder test: {n}", "PASS" if e["works"] else ("FAIL" if mandatory else "SKIP"), e.get("note", ""))
    d = caps["decode"]
    rec("H.264 donanım decode (ffmpeg testi)", "PASS" if d["h264_hw"]["works"] else "SKIP", d["h264_hw"]["note"])
    rec("HEVC donanım decode aygıtı", "PASS" if d["hevc_hw"]["works"] else "SKIP", d["hevc_hw"]["note"])
    rec("mpv hwdec listesi", "PASS" if caps["mpv_hwdecs"] else "WARN", ", ".join(caps["mpv_hwdecs"]) or "Pi hwdec yok → yazılımsal decode")
    disp = hardware.display_info()
    rec("HDMI ekran", "PASS" if disp["connected"] else "WARN",
        f"{disp['max_w']}x{disp['max_h']}" if disp["connected"] else "bağlı ekran algılanmadı (kiosk için gerekli)")
    outs = audio.list_outputs()
    rec("Ses çıkışları", "PASS" if outs else "WARN", ", ".join(o["label"] for o in outs) or "yok")
    if m == "pi5":
        rec("Pi 5 analog jak filtresi", "PASS" if not any(o["kind"] == "analog" for o in outs) else "FAIL")

    # pipeline test: synthesize clip -> analyze -> plan -> convert -> verify
    tmp = Path(tempfile.mkdtemp(prefix="avp-selftest-"))
    try:
        src = tmp / "src.mkv"
        p = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=29.97",
                            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "3", "-c:v", "libx264",
                            "-c:a", "mp3", "-ac", "2", str(src)], capture_output=True, text=True)
        rec("test videosu üretimi", "PASS" if p.returncode == 0 else "FAIL", p.stderr[-120:])
        if p.returncode == 0:
            s = media.analyze(src)
            rec("ffprobe analiz", "PASS" if s["video"] and s["video"]["width"] == 1920 else "FAIL", f"{s['video']['codec']} {s['video']['width']}x{s['video']['height']}@{s['video']['fps']}")
            for prof in ("auto", "keep", "1080p", "4k"):
                try:
                    pl = profiles.make_plan(s, m, prof, disp, None, ".mkv")
                    rec(f"plan: {prof}", "PASS", pl["summary"])
                except Exception as e:
                    rec(f"plan: {prof}", "FAIL", str(e))
            plan = profiles.make_plan(s, m, "1080p", disp, None, ".mkv")
            plan_sw = dict(plan)
            dst = tmp / "out.mp4"
            enc = converter.pick_encoder(plan, True)
            for encoder in dict.fromkeys([enc, "libx264"]):
                cmd = converter.build_cmd(str(src), str(dst), plan_sw, encoder)
                r = subprocess.run(cmd, capture_output=True, text=True)
                ok = r.returncode == 0
                good, msg = media.quick_check(dst) if ok else (False, r.stderr[-150:])
                rec(f"dönüştürme testi ({encoder})", "PASS" if ok and good else ("SKIP" if encoder != "libx264" else "FAIL"),
                    "doğrulandı" if ok and good else msg)
                dst.unlink(missing_ok=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # db + player state
    try:
        db.put("_selftest", 1); assert db.get("_selftest") == 1
        db.x("DELETE FROM settings WHERE k='_selftest'")
        rec("SQLite okuma/yazma", "PASS")
    except Exception as e:
        rec("SQLite okuma/yazma", "FAIL", str(e))
    if have("systemctl") and "--quick" not in sys.argv:
        for svc in ("avplayer-web", "avplayer-player"):
            r = subprocess.run(["systemctl", "is-active", svc], capture_output=True, text=True)
            rec(f"servis: {svc}", "PASS" if r.stdout.strip() == "active" else "FAIL", r.stdout.strip())
        time.sleep(2)
        try:
            st = json.loads(config.STATE_FILE.read_text())
            rec("player durum dosyası", "PASS" if time.time() - st["ts"] < 6 else "FAIL", st["state"])
        except Exception as e:
            rec("player durum dosyası", "FAIL", str(e))
        try:
            import urllib.request
            urllib.request.urlopen(f"http://127.0.0.1:{os.environ.get('AVP_PORT', '80')}/api/ping", timeout=5)
            rec("web API /api/ping", "PASS")
        except Exception as e:
            rec("web API /api/ping", "FAIL", str(e))
    return finish()


def finish() -> int:
    fails = [r for r in RESULTS if r[1] == "FAIL"]
    print(f"\nSonuç: {sum(r[1] == 'PASS' for r in RESULTS)} geçti, {len(fails)} başarısız, "
          f"{sum(r[1] in ('SKIP', 'WARN') for r in RESULTS)} atlandı/uyarı")
    try:
        (config.LOG_DIR / "selftest.json").write_text(json.dumps(RESULTS, ensure_ascii=False, indent=1))
    except Exception:
        pass
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
