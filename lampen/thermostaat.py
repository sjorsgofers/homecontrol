#!/usr/bin/env python3
"""
Nefit Easy-thermostaat voor de brug (brug.py) — optioneel.

Gebruikt het pakket aionefit-updated (pip install aionefit-updated). Er is geen officiële
koppeling; alle verkeer loopt via de servers van Bosch, net als de Nefit Easy-app.

In lampen-config.json (staat NIET in de repo):
  "nefit_serienummer":  "<serienummer, zonder spaties>",
  "nefit_toegangscode": "<toegangscode, zonder spaties>",
  "nefit_wachtwoord":   "<wachtwoord dat je in de Nefit Easy-app hebt ingesteld>"
Serienummer en toegangscode staan op de sticker achter op de thermostaat (en in de doos).

Testen:  python thermostaat.py --config <map>/lampen-config.json
         (leest alleen; verandert niets aan de temperatuur)
"""
import argparse
import asyncio
import json
import sys
import threading
import time

def _oudere_slixmpp():
    """aionefit geeft 'ssl_context' mee, wat pas vanaf slixmpp 1.9.1 bestaat. Nieuwere
    slixmpp heeft op Windows Rust nodig om te installeren; met 1.8.5 (puur Python) laten we
    dat argument hier weg. De versleuteling blijft gewoon aan (standaardinstellingen)."""
    import slixmpp
    oud = slixmpp.ClientXMPP.__init__
    if getattr(oud, "_aangepast", False):
        return
    try:
        import inspect
        if "ssl_context" in inspect.signature(slixmpp.xmlstream.XMLStream.__init__).parameters:
            return
    except Exception:
        pass

    def nieuw(self, *args, ssl_context=None, **kwargs):
        oud(self, *args, **kwargs)

    nieuw._aangepast = True
    slixmpp.ClientXMPP.__init__ = nieuw


PADEN = {
    "status": "/ecus/rrc/uiStatus",
    "buiten": "/system/sensors/temperatures/outdoor_t1",
}


class Thermostaat:
    """Eén Nefit Easy, te gebruiken vanuit gewone (niet-asynchrone) code."""

    def __init__(self, cfg, log=print):
        self.cfg = cfg
        self.log = log
        self.client = None
        self.antwoorden = {}
        self.wacht = {}
        self.fout = None
        self.stand = {}
        self.lus = asyncio.new_event_loop()
        threading.Thread(target=self.lus.run_forever, daemon=True).start()

    def _doe(self, coroutine, timeout=40):
        return asyncio.run_coroutine_threadsafe(coroutine, self.lus).result(timeout)

    async def _bericht(self, data):
        pad = data.get("id")
        if pad:
            self.antwoorden[pad] = data
            ev = self.wacht.get(pad)
            if ev:
                ev.set()

    async def _verbind(self):
        _oudere_slixmpp()
        from aionefit import NefitCore
        fouten = []

        async def mislukt(reden):
            fouten.append(str(reden))

        c = NefitCore(serial_number=str(self.cfg["nefit_serienummer"]).replace(" ", ""),
                      access_key=str(self.cfg["nefit_toegangscode"]).replace(" ", ""),
                      password=self.cfg["nefit_wachtwoord"], message_callback=self._bericht)
        c.failed_auth_handler = mislukt
        c.session_end_callback = None
        await c.connect()
        for _ in range(150):
            if c.xmppclient.connected_event.is_set() or fouten:
                break
            await asyncio.sleep(0.1)
        if fouten:
            raise RuntimeError("inloggen geweigerd: serienummer of toegangscode klopt niet")
        if not c.xmppclient.connected_event.is_set():
            raise RuntimeError("geen verbinding met de server van Bosch")
        self.client = c
        # Eerste vraag: controleert meteen of het wachtwoord klopt
        st = await self._vraag(PADEN["status"], 15)
        if st is None:
            fouten.append("geen antwoord")
        if any("password" in f for f in fouten) or st is None:
            raise RuntimeError("verbonden, maar de thermostaat antwoordt niet: wachtwoord onjuist?")

    async def _vraag(self, pad, timeout=10):
        ev = asyncio.Event()
        self.wacht[pad] = ev
        self.antwoorden.pop(pad, None)
        self.client.get(pad)
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self.wacht.pop(pad, None)
        return self.antwoorden.get(pad)

    def verbind(self):
        for n in ("nefit_serienummer", "nefit_toegangscode", "nefit_wachtwoord"):
            if not self.cfg.get(n):
                raise RuntimeError(f"{n} ontbreekt in lampen-config.json")
        try:
            self._doe(self._verbind(), timeout=60)
            self.fout = None
        except Exception as e:
            self.fout = str(e) or type(e).__name__
            self.client = None
            raise
        return self.lees()

    def lees(self):
        """Actuele stand: kamertemperatuur, ingestelde temperatuur, stand en of de ketel stookt."""
        if not self.client:
            return {"ok": False, "fout": self.fout or "niet verbonden"}

        async def lees():
            st = await self._vraag(PADEN["status"])
            if not st:
                return None
            v = st.get("value", {})
            uit = {
                "ok": True,
                "binnen": _getal(v.get("IHT")),
                "ingesteld": _getal(v.get("TSP")),
                "stand": "klok" if v.get("UMD") == "clock" else "handmatig",
                "stookt": v.get("BAI") in ("CH", "HW"),
                "warmwater": v.get("BAI") == "HW",
            }
            b = await self._vraag(PADEN["buiten"], 6)
            if b:
                uit["buiten"] = _getal(b.get("value"))
            return uit

        r = self._doe(lees(), timeout=30)
        if r:
            r["tijd"] = int(time.time())
            self.stand = r
            return r
        return {"ok": False, "fout": "thermostaat antwoordt niet"}

    def zet(self, graden):
        """Stelt de gewenste temperatuur in (5 t/m 30 °C, in halve graden)."""
        if not self.client:
            raise RuntimeError(self.fout or "thermostaat niet verbonden")
        g = round(max(5.0, min(30.0, float(graden))) * 2) / 2

        async def zet():
            self.client.set_temperature(g)

        self._doe(zet(), timeout=15)
        if self.stand:
            self.stand["ingesteld"] = g
        return g


def _getal(x):
    try:
        return round(float(x), 1)
    except (TypeError, ValueError):
        return None


def main():
    a = argparse.ArgumentParser(description="Test de koppeling met de Nefit Easy (leest alleen)")
    a.add_argument("--config", required=True)
    args = a.parse_args()
    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    t = Thermostaat(cfg)
    try:
        stand = t.verbind()
    except Exception as e:
        print(f"NIET GELUKT: {e}")
        sys.exit(1)
    print("Gelukt:", json.dumps(stand, ensure_ascii=False))


if __name__ == "__main__":
    main()
