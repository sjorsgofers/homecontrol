#!/usr/bin/env python3
"""
Brug op het thuisnetwerk — maakt het lampendashboard snel.

Het dashboard praat normaal met de web-app (apps-script.gs) en die met de Tuya Cloud;
dat kost per tik ruim een seconde. Deze brug draait op een apparaat thuis dat altijd
aan staat en stuurt de lampen rechtstreeks over het thuisnetwerk aan. De brug spreekt
dezelfde taal als de web-app, dus het dashboard kan ze door elkaar gebruiken; is de
brug niet bereikbaar, dan gebruikt het dashboard vanzelf weer de web-app.

  - indeling in ruimtes en lampnamen: van de web-app (action=state)
  - lokale sleutels en dp-nummers per lamp: van de web-app (action=lokaal)
  - adressen van de lampen: zoeken op het netwerk (tinytuya)
  - aan/uit, helderheid en status: rechtstreeks naar de lamp; lukt dat niet, dan via de web-app
  - scènes: altijd via de web-app

Installeren:  pip install tinytuya
Starten:      python brug.py --config <map>/lampen-config.json

lampen-config.json (staat NIET in de repo) bevat minstens:
  { "api": "<EXEC-URL>", "key": "<SLEUTEL>" }
en optioneel:
  "brug_poort": 8322,
  "brug_url": "https://<naam>.<tailnet>.ts.net:8443"   (adres waarop het dashboard de brug bereikt)

De brug luistert alleen op 127.0.0.1. Maak hem voor je eigen apparaten bereikbaar met
bijvoorbeeld Tailscale:  tailscale serve --bg --https=8443 http://127.0.0.1:8322
"""
import argparse
import hmac
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import tinytuya

AAN_CODES = ["switch_led", "switch_1", "switch"]
HELDER_CODES = ["bright_value_v2", "bright_value", "bright_value_1"]
TEMP_CODES = ["temp_value_v2", "temp_value"]
KLEUR_CODES = ["colour_data_v2", "colour_data"]
PEIL_ELKE = 5            # seconden tussen statusrondes langs de lampen
CLOUD_ELKE = 600         # seconden tussen het verversen van de indeling via de web-app
ZOEK_ELKE = 300          # seconden tussen zoekrondes naar lampen zonder (werkend) adres

CFG = {}
MAP = "."
LAMPEN = {}              # id -> Lamp
RUIMTES = []             # laatste indeling van de web-app (met de stand volgens de cloud)
SLOT = threading.Lock()
POOL = ThreadPoolExecutor(max_workers=24)


def log(tekst):
    regel = time.strftime("%Y-%m-%d %H:%M:%S ") + tekst
    try:
        with open(os.path.join(MAP, "brug.log"), "a", encoding="utf-8") as f:
            f.write(regel + "\n")
    except OSError:
        pass
    try:
        print(regel, flush=True)
    except Exception:
        pass


def cloud(params, timeout=60):
    """Vraagt iets aan de web-app (apps-script.gs)."""
    q = dict(params)
    q["key"] = CFG["key"]
    url = CFG["api"] + "?" + urllib.parse.urlencode(q)
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class Lamp:
    def __init__(self, info):
        self.id = info["id"]
        self.naam = (info.get("name") or "").strip()
        self.sleutel = info.get("key") or ""
        dps = info.get("dps") or {}
        self.dp_aan = next((str(dps[c]) for c in AAN_CODES if c in dps), None)
        self.dp_helder = next((str(dps[c]) for c in HELDER_CODES if c in dps), None)
        # Nieuwe lampen (dp 22) lopen van 10 tot 1000, oude (dp 2/3) van 25 tot 255
        self.bereik = (10, 1000) if self.dp_helder and int(self.dp_helder) >= 20 else (25, 255)
        # Warm/koud wit en kleur (alleen de nieuwe soort lampen: dp 21, 23 en 24)
        self.dp_modus = str(dps["work_mode"]) if "work_mode" in dps else None
        self.dp_temp = next((str(dps[c]) for c in TEMP_CODES if c in dps), None)
        self.dp_kleur = next((str(dps[c]) for c in KLEUR_CODES if c in dps), None)
        if not self.dp_modus or (self.dp_kleur and self.dp_kleur != "24"):
            self.dp_kleur = None
        self.modus = None
        self.temp = None         # procent: 0 = warm, 100 = koud
        self.tint = None         # kleurtoon 0-360 als de lamp op kleur staat
        self.hsv = None
        self.ip = None
        self.versie = None
        self.dev = None
        self.slot = threading.Lock()
        self.lokaal = False      # op dit moment rechtstreeks bereikbaar
        self.fouten = 0
        self.aan = False
        self.helder = None       # procent, of None als de lamp niet kan dimmen

    def naar_procent(self, waarde):
        lo, hi = self.bereik
        return max(1, min(100, round((float(waarde) - lo) / (hi - lo) * 99) + 1))

    def van_procent(self, procent):
        lo, hi = self.bereik
        p = max(1, min(100, float(procent)))
        return round(lo + (p - 1) / 99 * (hi - lo))

    def _apparaat(self):
        if self.dev is None:
            d = tinytuya.Device(self.id, self.ip, self.sleutel, version=float(self.versie or 3.3))
            d.set_socketPersistent(True)
            d.set_socketTimeout(2)
            d.set_socketRetryLimit(1)
            self.dev = d
        return self.dev

    def _verwerk(self, antwoord):
        """Leest de stand uit een antwoord van de lamp; geeft False bij een fout."""
        if not isinstance(antwoord, dict) or "dps" not in antwoord:
            return False
        dps = antwoord["dps"]
        if self.dp_aan in dps:
            self.aan = bool(dps[self.dp_aan])
        if self.dp_helder and self.dp_helder in dps:
            self.helder = self.naar_procent(dps[self.dp_helder])
        if self.dp_modus and self.dp_modus in dps:
            self.modus = dps[self.dp_modus]
        if self.dp_temp and self.dp_temp in dps:
            try:
                self.temp = max(0, min(100, round(float(dps[self.dp_temp]) / 10)))
            except (TypeError, ValueError):
                pass
        if self.dp_kleur and self.dp_kleur in dps:
            try:
                k = str(dps[self.dp_kleur])
                self.hsv = (int(k[0:4], 16), int(k[4:8], 16), int(k[8:12], 16))
            except (TypeError, ValueError):
                pass
        self.tint = self.hsv[0] if (self.modus == "colour" and self.hsv) else None
        if self.modus == "colour" and self.hsv:
            self.helder = max(1, min(100, round(self.hsv[2] / 10)))   # in kleur is de helderheid de v-waarde
        return True

    def _mislukt(self):
        self.fouten += 1
        try:
            if self.dev:
                self.dev.close()
        except Exception:
            pass
        self.dev = None
        if self.fouten >= 2:
            self.lokaal = False

    def peil(self):
        """Vraagt de stand rechtstreeks aan de lamp."""
        if not self.ip or not self.dp_aan:
            return
        with self.slot:
            try:
                if self._verwerk(self._apparaat().status()):
                    self.fouten = 0
                    self.lokaal = True
                else:
                    self._mislukt()
            except Exception:
                self._mislukt()

    @staticmethod
    def _kleurcode(h, s, v):
        return "%04x%04x%04x" % (int(h) % 360, max(0, min(1000, int(s))), max(10, min(1000, int(v))))

    def zet(self, aan, helder, temp=None, kleur=None):
        """Schakelt de lamp rechtstreeks. Geeft True als dat gelukt is."""
        if not self.lokaal:
            return False
        waarden = {}
        if aan is not None:
            waarden[self.dp_aan] = bool(aan)
        if temp is not None and self.dp_temp and self.dp_modus:
            waarden[self.dp_modus] = "white"
            waarden[self.dp_temp] = round(max(0, min(100, float(temp))) * 10)
        elif kleur is not None and self.dp_kleur:
            v = round(helder * 10) if helder is not None else (self.hsv[2] if self.modus == "colour" and self.hsv else 1000)
            waarden[self.dp_modus] = "colour"
            waarden[self.dp_kleur] = self._kleurcode(kleur, 1000, v)
        if helder is not None and kleur is None:
            if self.modus == "colour" and self.dp_kleur and self.hsv and temp is None:
                # Lamp staat op kleur: helderheid zit dan in de kleurwaarde
                waarden[self.dp_kleur] = self._kleurcode(self.hsv[0], self.hsv[1], round(helder * 10))
            elif self.dp_helder:
                waarden[self.dp_helder] = self.van_procent(helder)
        if aan is None and waarden and (helder is None or helder > 0):
            waarden[self.dp_aan] = True
        if not waarden:
            return True
        with self.slot:
            for poging in (1, 2):
                try:
                    antwoord = self._apparaat().set_multiple_values(waarden, nowait=False)
                    if isinstance(antwoord, dict) and antwoord.get("Error"):
                        raise RuntimeError(antwoord["Error"])
                    self._verwerk(antwoord)
                    # De lamp bevestigt niet altijd alle waarden: zelf alvast bijwerken
                    if self.dp_aan in waarden:
                        self.aan = waarden[self.dp_aan]
                    if self.dp_helder in waarden:
                        self.helder = self.naar_procent(waarden[self.dp_helder])
                    if self.dp_modus in waarden:
                        self.modus = waarden[self.dp_modus]
                    if self.dp_temp in waarden:
                        self.temp = round(waarden[self.dp_temp] / 10)
                    if self.dp_kleur in waarden:
                        k = waarden[self.dp_kleur]
                        self.hsv = (int(k[0:4], 16), int(k[4:8], 16), int(k[8:12], 16))
                        self.helder = max(1, round(self.hsv[2] / 10))
                    self.tint = self.hsv[0] if (self.modus == "colour" and self.hsv) else None
                    self.fouten = 0
                    return True
                except Exception:
                    self._mislukt()
            return False


# ------------------------------- gegevens ophalen -------------------------------

def adressen_pad():
    return os.path.join(MAP, "brug-adressen.json")


def laad_lampen():
    """Haalt sleutels en dp-nummers op bij de web-app en bouwt de lijst met lampen."""
    r = cloud({"action": "lokaal"})
    if not r.get("ok"):
        raise RuntimeError(r.get("fout") or "web-app gaf geen lokale gegevens")
    try:
        with open(adressen_pad(), encoding="utf-8") as f:
            bekend = json.load(f)
    except (OSError, ValueError):
        bekend = {}
    nieuw = {}
    for info in r.get("apparaten", []):
        oud = LAMPEN.get(info["id"])
        lamp = Lamp(info)
        adres = bekend.get(lamp.id) or {}
        lamp.ip, lamp.versie = adres.get("ip"), adres.get("versie")
        if oud and oud.sleutel == lamp.sleutel and oud.ip:
            lamp.ip, lamp.versie = oud.ip, oud.versie
            lamp.aan, lamp.helder, lamp.lokaal = oud.aan, oud.helder, oud.lokaal
        nieuw[lamp.id] = lamp
    with SLOT:
        LAMPEN.clear()
        LAMPEN.update(nieuw)
    log(f"{len(nieuw)} lampen bekend, {sum(1 for l in nieuw.values() if l.ip)} met een adres")


def laad_ruimtes():
    """Haalt de indeling in ruimtes (en de stand volgens de cloud) op bij de web-app."""
    global RUIMTES
    r = cloud({"action": "state", "_": int(time.time())})
    if not r.get("ok"):
        raise RuntimeError(r.get("fout") or "web-app gaf geen stand")
    RUIMTES = r.get("ruimtes", [])


def zoek_adressen():
    """Zoekt de lampen op het netwerk en onthoudt adres en protocolversie."""
    gevonden = tinytuya.deviceScan(False, 12)
    veranderd = 0
    for v in gevonden.values():
        lamp = LAMPEN.get(v.get("gwId"))
        if not lamp:
            continue
        ip, versie = v.get("ip"), str(v.get("version") or "3.3")
        if lamp.ip != ip or lamp.versie != versie:
            with lamp.slot:
                lamp.ip, lamp.versie, lamp.dev, lamp.fouten = ip, versie, None, 0
            veranderd += 1
    try:
        with open(adressen_pad(), "w", encoding="utf-8") as f:
            json.dump({l.id: {"ip": l.ip, "versie": l.versie} for l in LAMPEN.values() if l.ip}, f)
    except OSError:
        pass
    if veranderd:
        log(f"zoekronde: {veranderd} adres(sen) nieuw of gewijzigd")


def peil_alles():
    list(POOL.map(lambda l: l.peil(), list(LAMPEN.values())))


def achtergrond():
    """Houdt de stand, de indeling en de adressen bij."""
    laatst_cloud = time.time()
    laatst_zoek = 0
    laatst_gemeld = None
    while True:
        try:
            peil_alles()
            lokaal = sum(1 for l in LAMPEN.values() if l.lokaal)
            if lokaal != laatst_gemeld:
                log(f"{lokaal} van {len(LAMPEN)} lampen rechtstreeks bereikbaar")
                laatst_gemeld = lokaal
            nu = time.time()
            niet_lokaal = lokaal < len(LAMPEN)
            if nu - laatst_cloud > (60 if niet_lokaal else CLOUD_ELKE):
                laatst_cloud = nu
                laad_ruimtes()
            if niet_lokaal and nu - laatst_zoek > ZOEK_ELKE:
                laatst_zoek = nu
                zoek_adressen()
        except Exception as e:
            log(f"achtergrond: {e}")
        time.sleep(PEIL_ELKE)


THERMO = None


def thermostaat_lus():
    """Optioneel: houdt de stand van een Nefit Easy bij (zie thermostaat.py)."""
    global THERMO
    if not CFG.get("nefit_serienummer"):
        return
    try:
        from thermostaat import Thermostaat
    except Exception as e:
        log(f"thermostaat niet beschikbaar: {e}")
        return
    while THERMO is None:
        try:
            t = Thermostaat(CFG, log)
            t.verbind()
            THERMO = t
            log("thermostaat verbonden")
        except Exception as e:
            log(f"thermostaat: {e}")
            time.sleep(900)
    while True:
        time.sleep(60)
        try:
            THERMO.lees()
        except Exception as e:
            log(f"thermostaat lezen: {e}")


ZUIGER = None
ZUIGER_STAND = {"ok": False, "fout": "niet gekoppeld"}


def stofzuiger_lus():
    """Optioneel: Eufy-robotstofzuiger (zie stofzuiger.py)."""
    global ZUIGER, ZUIGER_STAND
    if not CFG.get("eufy_email"):
        return
    try:
        from stofzuiger import Stofzuiger
    except Exception as e:
        log(f"stofzuiger niet beschikbaar: {e}")
        return
    while ZUIGER is None:
        try:
            z = Stofzuiger(CFG, MAP, log)
            z.verbind()
            ZUIGER = z
            log("stofzuiger verbonden: " + str(z.info.get("naam")))
        except Exception as e:
            ZUIGER_STAND = {"ok": False, "fout": str(e)}
            log(f"stofzuiger: {e}")
            time.sleep(1800)       # niet vaak opnieuw proberen: Eufy blokkeert bij te veel inlogpogingen
    while True:
        try:
            ZUIGER_STAND = ZUIGER.stand()
        except Exception as e:
            log(f"stofzuiger lezen: {e}")
        time.sleep(20)


def meld_adres():
    """Vertelt de web-app op welk adres het dashboard de brug kan bereiken."""
    while True:
        if CFG.get("brug_url"):
            try:
                cloud({"action": "brug", "url": CFG["brug_url"]})
            except Exception as e:
                log(f"adres melden mislukt: {e}")
        time.sleep(6 * 3600)


# --------------------------------- opdrachten ---------------------------------

def stand():
    uit = []
    for r in RUIMTES:
        lampen = []
        for c in r.get("lamps", []):
            lamp = LAMPEN.get(c["id"])
            if lamp and lamp.lokaal:
                lampen.append({"id": lamp.id, "name": c.get("name") or lamp.naam, "online": True,
                               "on": lamp.aan, "bright": lamp.helder,
                               "temp": lamp.temp if lamp.dp_temp else None,
                               "rgb": bool(lamp.dp_kleur), "tint": lamp.tint})
            else:
                lampen.append(c)
        uit.append({"id": r["id"], "name": r["name"], "lamps": lampen})
    return uit


def werk_cloudstand_bij(ids, aan, helder):
    """Na schakelen via de web-app: de onthouden cloudstand alvast bijwerken."""
    for r in RUIMTES:
        for c in r.get("lamps", []):
            if c["id"] in ids and c.get("online"):
                if aan is not None:
                    c["on"] = aan
                if helder is not None:
                    c["on"] = True
                    if c.get("bright") is not None:
                        c["bright"] = helder


def schakel(ids, aan, helder, temp=None, kleur=None):
    def een(i):
        lamp = LAMPEN.get(i)
        return i, bool(lamp and lamp.zet(aan, helder, temp, kleur))

    resultaat = {}
    rest = []
    for i, gelukt in POOL.map(een, ids):
        if gelukt:
            resultaat[i] = {"id": i, "ok": True, "via": "lokaal"}
        else:
            rest.append(i)
    if rest:
        q = {"action": "set", "ids": ",".join(rest)}
        if aan is not None:
            q["on"] = "true" if aan else "false"
        if helder is not None:
            q["bright"] = str(helder)
        if temp is not None:
            q["temp"] = str(temp)
        if kleur is not None:
            q["kleur"] = str(kleur)
        try:
            r = cloud(q)
            for x in r.get("resultaat", []):
                x["via"] = "cloud"
                resultaat[x["id"]] = x
            werk_cloudstand_bij(set(rest), aan, helder)
        except Exception as e:
            for i in rest:
                resultaat[i] = {"id": i, "ok": False, "fout": str(e)}
    return [resultaat.get(i, {"id": i, "ok": False, "fout": "onbekende lamp"}) for i in ids]


def verwerk(p):
    actie = p.get("action") or "state"
    if actie == "ping":
        return {"ok": True, "brug": True}
    if not hmac.compare_digest(str(p.get("key") or ""), str(CFG["key"])):
        return {"ok": False, "fout": "Onjuiste sleutel"}
    if actie == "set":
        ids = p.get("ids") or []
        if isinstance(ids, str):
            ids = [i for i in ids.split(",") if i]
        aan = p.get("on")
        if aan in (None, ""):
            aan = None
        elif not isinstance(aan, bool):
            aan = str(aan).lower() in ("true", "1")
        helder = p.get("bright")
        helder = None if helder in (None, "") else float(helder)
        temp = None if p.get("temp") in (None, "") else float(p["temp"])
        kleur = None if p.get("kleur") in (None, "") else float(p["kleur"])
        return {"ok": True, "resultaat": schakel(ids, aan, helder, temp, kleur), "brug": True}
    if actie == "thermostaat":
        if THERMO is None:
            return {"ok": True, "brug": True, "thermostaat": {"ok": False, "fout": "niet gekoppeld"}}
        if p.get("stand") in ("klok", "handmatig"):
            THERMO.zet_stand(p["stand"])
            time.sleep(2)          # de thermostaat even de tijd geven en dan de echte stand lezen
            THERMO.lees()
        if p.get("graden") not in (None, ""):
            THERMO.zet(float(p["graden"]))
        return {"ok": True, "brug": True, "thermostaat": THERMO.stand or THERMO.lees()}
    if actie == "stofzuiger":
        global ZUIGER_STAND
        if ZUIGER is None:
            return {"ok": True, "brug": True, "stofzuiger": ZUIGER_STAND}
        if p.get("opdracht"):
            ZUIGER.opdracht(str(p["opdracht"]))
            time.sleep(3)          # even wachten tot de stofzuiger zijn nieuwe stand meldt
            ZUIGER_STAND = ZUIGER.stand()
        return {"ok": True, "brug": True, "stofzuiger": ZUIGER_STAND}
    if actie in ("scene", "scenes", "agenda"):
        q = {"action": actie}
        if p.get("name"):
            q["name"] = p["name"]
        return cloud(q)
    if actie == "refresh":
        cloud({"action": "refresh"})
        laad_lampen()
        laad_ruimtes()
        POOL.submit(zoek_adressen)
    lokaal = sum(1 for l in LAMPEN.values() if l.lokaal)
    return {"ok": True, "ruimtes": stand(), "brug": True, "lokaal": lokaal, "totaal": len(LAMPEN)}


class Verzoek(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _kop(self, code, soort="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", soort)
        self.send_header("Cache-Control", "no-store")
        # Het dashboard staat op een ander adres (GitHub Pages) en moet de brug mogen aanroepen
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _antwoord(self, p):
        try:
            uit = verwerk(p)
        except Exception as e:
            uit = {"ok": False, "fout": str(e)}
        data = json.dumps(uit).encode("utf-8")
        self._kop(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self._kop(204)
        self.end_headers()

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        self._antwoord({k: v[0] for k, v in q.items()})

    def do_POST(self):
        try:
            lengte = int(self.headers.get("Content-Length") or 0)
            p = json.loads(self.rfile.read(lengte).decode("utf-8") or "{}")
        except Exception:
            p = {}
        self._antwoord(p if isinstance(p, dict) else {})


def main():
    global CFG, MAP
    a = argparse.ArgumentParser(description="Brug voor het lampendashboard op het thuisnetwerk")
    a.add_argument("--config", required=True, help="pad naar lampen-config.json (met api en key)")
    a.add_argument("--poort", type=int, help="poort op 127.0.0.1 (standaard 8322 of brug_poort uit de config)")
    args = a.parse_args()
    MAP = os.path.dirname(os.path.abspath(args.config))
    with open(args.config, encoding="utf-8") as f:
        CFG = json.load(f)
    if not CFG.get("api") or not CFG.get("key"):
        sys.exit("lampen-config.json mist 'api' of 'key'")
    poort = args.poort or int(CFG.get("brug_poort") or 8322)

    while True:   # wachten tot de web-app bereikbaar is (bijvoorbeeld vlak na het opstarten van de pc)
        try:
            laad_lampen()
            laad_ruimtes()
            break
        except Exception as e:
            log(f"web-app nog niet bereikbaar: {e}")
            time.sleep(30)
    if any(not l.ip for l in LAMPEN.values()):
        try:
            zoek_adressen()
        except Exception as e:
            log(f"zoeken mislukt: {e}")
    peil_alles()

    threading.Thread(target=achtergrond, daemon=True).start()
    threading.Thread(target=meld_adres, daemon=True).start()
    threading.Thread(target=thermostaat_lus, daemon=True).start()
    threading.Thread(target=stofzuiger_lus, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", poort), Verzoek)
    log(f"brug luistert op 127.0.0.1:{poort}")
    server.serve_forever()


if __name__ == "__main__":
    main()
