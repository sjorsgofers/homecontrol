#!/usr/bin/env python3
"""
Eufy-robotstofzuiger voor de brug (brug.py) — optioneel.

Eufy heeft geen officiële koppeling. Dit gebruikt de onofficiële code van
https://github.com/thjont/eufy-clean (zelf ophalen met git clone, staat niet in deze repo)
en werkt alleen met modellen die via Eufy's berichtendienst (MQTT) lopen. Het kan stuk
gaan als Eufy iets wijzigt.

In lampen-config.json (staat NIET in de repo):
  "eufy_email":      "<e-mailadres van je Eufy-account>",
  "eufy_wachtwoord": "<wachtwoord>",
  "eufy_code":       "<map met de checkout van eufy-clean>",   (standaard: eufy-clean naast de config)
  "eufy_apparaat":   "<naam of id>"                            (optioneel; standaard de eerste)

Benodigd:  pip install aiohttp paho-mqtt protobuf
Testen:    python stofzuiger.py --config <map>/lampen-config.json
           (logt in en toont de gevonden stofzuigers en hun stand; stuurt geen opdrachten)
"""
import argparse
import asyncio
import enum
import json
import os
import sys
import threading
import types


def _nep_homeassistant():
    """De Eufy-code is geschreven voor Home Assistant en importeert daar een paar namen uit.
    Die zijn hier niet nodig; dit zet lege vervangers neer zodat Home Assistant niet
    geïnstalleerd hoeft te worden."""
    try:
        import homeassistant  # noqa: F401  (echt aanwezig: niets doen)
        return
    except ImportError:
        pass

    def module(naam):
        m = types.ModuleType(naam)
        sys.modules[naam] = m
        return m

    module("homeassistant")
    module("homeassistant.components")
    module("homeassistant.config_entries").ConfigEntry = type("ConfigEntry", (), {})
    module("homeassistant.core").HomeAssistant = type("HomeAssistant", (), {})

    class Platform(str, enum.Enum):
        VACUUM = "vacuum"

    class VacuumActivity(str, enum.Enum):
        CLEANING = "cleaning"
        DOCKED = "docked"
        IDLE = "idle"
        PAUSED = "paused"
        RETURNING = "returning"
        ERROR = "error"

    module("homeassistant.const").Platform = Platform
    module("homeassistant.components.vacuum").VacuumActivity = VacuumActivity


STAND_NL = {
    "cleaning": "Aan het zuigen", "docked": "In het station", "idle": "Stil",
    "paused": "Gepauzeerd", "returning": "Onderweg naar het station", "error": "Storing",
}


class Stofzuiger:
    """Eén Eufy-stofzuiger, te bedienen vanuit gewone (niet-asynchrone) code."""

    def __init__(self, cfg, map_, log=print):
        self.cfg = cfg
        self.map = map_
        self.log = log
        self.apparaat = None
        self.info = {}
        self.apparaten = []
        self.fout = None
        self.lus = asyncio.new_event_loop()
        threading.Thread(target=self.lus.run_forever, daemon=True).start()

    def _doe(self, coroutine, timeout=60):
        return asyncio.run_coroutine_threadsafe(coroutine, self.lus).result(timeout)

    def verbind(self):
        """Logt in bij Eufy en maakt verbinding met de stofzuiger. Geeft de lijst met apparaten."""
        if not self.cfg.get("eufy_email") or not self.cfg.get("eufy_wachtwoord"):
            raise RuntimeError("eufy_email en eufy_wachtwoord ontbreken in lampen-config.json")
        code = self.cfg.get("eufy_code") or os.path.join(self.map, "eufy-clean")
        if not os.path.isdir(os.path.join(code, "custom_components", "robovac_mqtt")):
            raise RuntimeError(f"Eufy-code niet gevonden in {code}")
        # De Eufy-code schrijft twee sleutelbestanden naar de werkmap: die horen bij de config
        werk = os.path.join(self.map, "eufy-data")
        os.makedirs(werk, exist_ok=True)
        os.chdir(werk)
        _nep_homeassistant()
        if code not in sys.path:
            sys.path.insert(0, code)
        from custom_components.robovac_mqtt.EufyClean import EufyClean

        async def start():
            eufy = EufyClean(self.cfg["eufy_email"], self.cfg["eufy_wachtwoord"])
            await eufy.init()
            apparaten = await eufy.get_devices()
            self.apparaten = [{"id": d.get("deviceId"), "naam": d.get("deviceName"), "model": d.get("deviceModel"),
                               "modelnaam": d.get("deviceModelName"), "soort": d.get("apiType")} for d in apparaten]
            if not apparaten:
                raise RuntimeError("Eufy gaf geen stofzuigers terug die via MQTT werken")
            wens = str(self.cfg.get("eufy_apparaat") or "").strip().lower()
            gekozen = next((d for d in apparaten if wens and wens in (str(d.get("deviceId")).lower(),
                                                                       str(d.get("deviceName")).lower())), apparaten[0])
            self.info = next(a for a in self.apparaten if a["id"] == gekozen.get("deviceId"))
            self.apparaat = await eufy.init_device(gekozen["deviceId"])
            await self.apparaat.connect()

        try:
            self._doe(start(), timeout=90)
            self.fout = None
        except Exception as e:
            self.fout = str(e) or type(e).__name__
            self.apparaat = None
            raise
        return self.apparaten

    def stand(self):
        """De huidige stand: naam, accu en wat hij aan het doen is."""
        if not self.apparaat:
            return {"ok": False, "fout": self.fout or "niet verbonden"}

        async def lees():
            uit = {"ok": True, "naam": self.info.get("naam"), "model": self.info.get("modelnaam") or self.info.get("model")}
            try:
                uit["accu"] = await self.apparaat.get_battery_level()
            except Exception:
                uit["accu"] = None
            try:
                s = await self.apparaat.get_work_status()
                s = getattr(s, "value", s)
                uit["stand"] = str(s)
                uit["tekst"] = STAND_NL.get(str(s), str(s))
            except Exception:
                uit["stand"], uit["tekst"] = None, "Onbekend"
            return uit

        return self._doe(lees(), timeout=15)

    def opdracht(self, wat):
        """wat: 'start', 'pauze', 'verder', 'stop' of 'thuis'."""
        if not self.apparaat:
            raise RuntimeError(self.fout or "stofzuiger niet verbonden")
        acties = {"start": "auto_clean", "pauze": "pause", "verder": "play", "stop": "stop", "thuis": "go_home"}
        if wat not in acties:
            raise ValueError(f"onbekende opdracht: {wat}")
        self._doe(getattr(self.apparaat, acties[wat])(), timeout=20)
        return True


def main():
    a = argparse.ArgumentParser(description="Test de koppeling met de Eufy-stofzuiger (stuurt geen opdrachten)")
    a.add_argument("--config", required=True, help="pad naar lampen-config.json")
    args = a.parse_args()
    map_ = os.path.dirname(os.path.abspath(args.config))
    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    s = Stofzuiger(cfg, map_)
    try:
        apparaten = s.verbind()
    except Exception as e:
        print(f"NIET GELUKT: {e}")
        sys.exit(1)
    print(f"Ingelogd. {len(apparaten)} stofzuiger(s) gevonden:")
    for d in apparaten:
        print(f"  - {d['naam']} | model {d['model']} ({d['modelnaam']}) | {d['soort']}")
    import time
    time.sleep(3)   # even wachten op de eerste berichten van de stofzuiger
    print("Stand:", json.dumps(s.stand(), ensure_ascii=False))


if __name__ == "__main__":
    main()
