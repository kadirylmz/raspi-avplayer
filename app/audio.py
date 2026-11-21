"""ALSA output detection (HDMI / analog 3.5mm / USB). Only existing outputs are offered."""
import re
import subprocess
from pathlib import Path

from . import hardware

KIND_LABEL = {"hdmi": "HDMI AUDIO", "analog": "ANALOG AUDIO (3.5 MM JACK)", "usb": "USB AUDIO"}
CARD_RE = re.compile(r"^card (\d+): (\S+) \[(.*?)\], device (\d+): (.*?) \[(.*?)\]")


def classify(card_id: str, card_name: str, dev_name: str, is_usb: bool) -> str | None:
    blob = f"{card_id} {card_name} {dev_name}".lower()
    if is_usb:
        return "usb"
    if "hdmi" in blob:
        return "hdmi"
    if "headphones" in blob or "bcm2835" in blob or "analog" in blob:
        return "analog"
    return None


def parse_aplay(text: str, model: str, usb_cards: set[str] | None = None) -> list[dict]:
    usb_cards = usb_cards or set()
    outs, hdmi_n = [], 0
    for line in text.splitlines():
        m = CARD_RE.match(line)
        if not m:
            continue
        num, cid, cname, dev, _dn, dname = m.groups()
        kind = classify(cid, cname, dname, num in usb_cards)
        if kind is None:
            continue
        if kind == "analog" and model == "pi5":
            continue  # Raspberry Pi 5 has no 3.5 mm audio jack
        if kind == "hdmi":
            hdmi_n += 1
        oid = f"{kind}:{cid}:{dev}"
        label = KIND_LABEL[kind]
        if kind == "hdmi":
            label += f" {cname.replace('vc4-hdmi-', 'HDMI ')}" if "vc4" in cname else f" ({cname})"
        elif kind == "usb":
            label += f" – {cname}"
        # On Raspberry Pi vc4hdmi0/vc4hdmi1, ALSA "plughw:" fails unless IEC958 framing is injected
        # via "hdmi:CARD=..." (or sysdefault:CARD=...). Using sysdefault / hdmi avoids "[ao] Failed to initialize audio driver 'alsa'".
        mpv_dev = f"alsa/hdmi:CARD={cid},DEV={dev}" if kind == "hdmi" else f"alsa/plughw:CARD={cid},DEV={dev}"
        outs.append({"id": oid, "kind": kind, "label": label, "card": cid, "device": int(dev),
                     "mpv_device": mpv_dev, "fallback_device": f"alsa/sysdefault:CARD={cid}"})
    return outs


def list_outputs(model: str | None = None) -> list[dict]:
    model = model or hardware.model()
    try:
        text = subprocess.run(["aplay", "-l"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    usb = set()
    for d in Path("/proc/asound").glob("card*"):
        if (d / "usbid").exists():
            usb.add(d.name.replace("card", ""))
    return parse_aplay(text, model, usb)


def resolve(setting: str, outs: list[dict]) -> dict | None:
    """Pick the configured output; 'auto' or a missing one falls back USB > HDMI > analog."""
    for o in outs:
        if o["id"] == setting:
            return o
    for kind in ("usb", "hdmi", "analog"):
        for o in outs:
            if o["kind"] == kind:
                return o
    return None
