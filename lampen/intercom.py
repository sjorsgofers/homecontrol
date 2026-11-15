#!/usr/bin/env python3
"""
Ring Intercom voor de brug (brug.py) — optioneel.

Laat het dashboard horen dat er beneden wordt aangebeld en de deur openen. Ring heeft geen
officiële koppeling: dit gebruikt de onofficiële bibliotheek python-ring-doorbell en loopt
volledig via de dienst van Ring. Het aanbelsignaal komt als pushbericht binnen en kan een
enkele keer gemist worden; gebruik dan de Ring-app.

Veiligheid: de deur gaat alleen open in de eerste minuten nadat er is aangebeld
("ring_venster" in lampen-config.json, in seconden, standaard 120). Daarbuiten weigert de
brug, ook met de juiste sleutel.

Eenmalig inloggen (jij typt zelf je wachtwoord en de code; er wordt alleen een token bewaard):
    python intercom.py --config <map>/lampen-config.json --inloggen
Dat schrijft ring-token.json naast de config (staat NIET in de repo). Zolang dat bestand
bestaat doet de brug mee.

Testen (opent de deur niet):
    python intercom.py --config <map>/lampen-config.json

Benodigd:  pip install ring-doorbell
"""
import argparse
import asyncio
import getpass
import json
import os
import sys
import threading
import time

AGENT = "LampenDashboard/1.0"
VENSTER = 120            # seconden na het aanbellen waarin de deur open mag
VERVERS_ELKE = 12 * 3600  # zo vaak opnieuw aanmelden voor pushberichten (ze vallen anders soms stil)


def _pad(map_, naam):
    return os.path.join(map_, naam)


def _lees(pad):
    try:
        with open(pad, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _schrijf(pad, data):
    tijdelijk = pad + ".nieuw"
    with open(tijdelijk, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tijdelijk, pad)


def gekoppeld(map_):
    return os.path.isfile(_pad(map_, "ring-token.json"))


class Intercom:
    """Eén Ring Intercom, te bedienen vanuit gewone (niet-asynchrone) code."""

    def __init__(self, cfg, map_, log=print):
        self.cfg = cfg
        self.map = map_
        self.log = log
        self.venster = int(cfg.get("ring_venster") or VENSTER)
        self.ring = None
        self.apparaat = None
        self.luisteraar = None
        self.gebeld = None       # tijdstip (epoch) van de laatste keer aanbellen
        self.geopend = None      # tijdstip waarop de deur via het dashboard is geopend
        self.fout = None
        self.lus = asyncio.new_event_loop()
        threading.Thread(target=self.lus.run_forever, daemon=True).start()

    def _doe(self, coroutine, timeout=40):
        return asyncio.run_coroutine_threadsafe(coroutine, self.lus).result(timeout)

    # ---- verbinden ----

    def verbind(self):
        from ring_doorbell import Auth, Ring
        from ring_doorbell.const import INTERCOM_KINDS

        token = _lees(_pad(self.map, "ring-token.json"))
        if not token:
            raise RuntimeError("niet ingelogd bij Ring (draai intercom.py --inloggen)")

        async def start():
            auth = Auth(AGENT, token, lambda t: _schrijf(_pad(self.map, "ring-token.json"), t))
            self.ring = Ring(auth)
            await self.ring.async_create_session()
            await self.ring.async_update_data()
            intercoms = [d for d in self.ring.devices().other if d.kind in INTERCOM_KINDS]
            if not intercoms:
                raise RuntimeError("geen Ring Intercom gevonden op dit account")
            wens = str(self.cfg.get("ring_apparaat") or "").strip().lower()
            self.apparaat = next((d for d in intercoms if wens and wens in (str(d.device_api_id), d.name.lower())),
                                 intercoms[0])
            await self._luister()

        try:
            self._doe(start(), timeout=90)
            self.fout = None
        except Exception as e:
            self.fout = str(e) or type(e).__name__
            self.apparaat = None
            raise
        return self.apparaat.name

    async def _luister(self):
        from ring_doorbell import RingEventListener
        if self.luisteraar:
            try:
                await self.luisteraar.stop()
            except Exception:
                pass
        pad = _pad(self.map, "ring-fcm.json")
        self.luisteraar = RingEventListener(self.ring, _lees(pad), lambda c: _schrijf(pad, c))
        self.luisteraar.add_notification_callback(self._gebeurtenis)
        if not await self.luisteraar.start(timeout=30):
            raise RuntimeError("aanmelden voor pushberichten van Ring is niet gelukt")

    def _gebeurtenis(self, g):
        try:
            if g.kind != "ding" or getattr(g, "is_update", False):
                return
            if self.apparaat is not None and int(g.doorbot_id) != int(self.apparaat.device_api_id):
                return
            # Het tijdstip van Ring zelf telt: een bericht dat te laat binnenkomt opent geen venster
            wanneer = min(float(g.now), time.time())
            if time.time() - wanneer > self.venster:
                self.log("intercom: aanbelbericht kwam te laat binnen, genegeerd")
                return
            if self.gebeld is None or wanneer > self.gebeld:
                self.gebeld = wanneer
                self.geopend = None
                self.log("intercom: er wordt aangebeld")
        except Exception as e:
            self.log(f"intercom: bericht niet begrepen ({type(e).__name__})")

    def ververs(self):
        """Opnieuw aanmelden voor pushberichten en de gegevens bijwerken."""
        async def doe():
            await self.ring.async_update_data()
            await self._luister()
        self._doe(doe(), timeout=90)

    # ---- gebruik ----

    def open_mag(self):
        return self.gebeld is not None and 0 <= time.time() - self.gebeld <= self.venster

    def stand(self):
        if not self.apparaat:
            return {"ok": False, "fout": self.fout or "niet gekoppeld"}
        nu = time.time()
        return {"ok": True, "naam": self.apparaat.name, "accu": self.apparaat.battery_life,
                "gebeld": int(self.gebeld * 1000) if self.gebeld else None,
                "open_mag": self.open_mag(),
                "rest": max(0, int(self.venster - (nu - self.gebeld))) if self.open_mag() else 0,
                "geopend": bool(self.geopend and self.gebeld and self.geopend >= self.gebeld)}

    def open(self):
        """Opent de deur beneden — alleen kort nadat er is aangebeld."""
        if not self.apparaat:
            raise RuntimeError(self.fout or "intercom niet gekoppeld")
        if not self.open_mag():
            raise RuntimeError("De deur kan alleen open vlak nadat er is aangebeld")
        if not self._doe(self.apparaat.async_open_door(), timeout=30):
            raise RuntimeError("Ring heeft de deur niet geopend")
        self.geopend = time.time()
        self.log("intercom: deur geopend via het dashboard")
        return True


def inloggen(map_):
    """Vraagt zelf om e-mailadres, wachtwoord en code; bewaart alleen het token."""
    from ring_doorbell import Auth, AuthenticationError, Requires2FAError

    async def doe():
        email = input("E-mailadres van je Ring-account: ").strip()
        wachtwoord = getpass.getpass("Wachtwoord (wordt niet getoond en niet bewaard): ")
        auth = Auth(AGENT, None, lambda t: _schrijf(_pad(map_, "ring-token.json"), t))
        try:
            try:
                await auth.async_fetch_token(email, wachtwoord)
            except Requires2FAError:
                code = input("Verificatiecode die Ring je net stuurde (sms/mail/app): ").strip()
                await auth.async_fetch_token(email, wachtwoord, code)
        except AuthenticationError as e:
            print(f"NIET GELUKT: Ring weigert de inlog ({e})")
            return False
        finally:
            await auth.async_close()
        return True

    if asyncio.run(doe()):
        print("Ingelogd. Het token staat in ring-token.json naast de config.")
        return True
    return False


def main():
    a = argparse.ArgumentParser(description="Ring Intercom koppelen of testen (opent de deur niet)")
    a.add_argument("--config", required=True, help="pad naar lampen-config.json")
    a.add_argument("--inloggen", action="store_true", help="eenmalig inloggen bij Ring")
    a.add_argument("--luister", type=int, default=0, help="zoveel seconden wachten op aanbellen (test)")
    args = a.parse_args()
    map_ = os.path.dirname(os.path.abspath(args.config))
    if args.inloggen:
        sys.exit(0 if inloggen(map_) else 1)
    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    i = Intercom(cfg, map_)
    try:
        naam = i.verbind()
    except Exception as e:
        print(f"NIET GELUKT: {e}")
        sys.exit(1)
    print(f"Verbonden met intercom '{naam}'. Stand:", json.dumps(i.stand(), ensure_ascii=False))
    if args.luister:
        print(f"Ik luister {args.luister} seconden; bel beneden aan om te testen...")
        einde = time.time() + args.luister
        while time.time() < einde and not i.gebeld:
            time.sleep(1)
        print("Aangebeld: ja" if i.gebeld else "Geen aanbelbericht ontvangen")


if __name__ == "__main__":
    main()
