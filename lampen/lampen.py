#!/usr/bin/env python3
"""
Lampen op de Nest Hub — serveert de lampenpagina en houdt hem 24/7 op de Hub.

Draait op een apparaat dat altijd aan staat en op hetzelfde wifi zit als de Hub
(Raspberry Pi, NAS, mini-pc, oude laptop). Het:
  1. serveert index.html uit deze map op het thuisnetwerk;
  2. kijkt elke minuut wat de Hub laat zien en zet de pagina terug zodra hij
     naar klok/fotolijst springt. Speelt er muziek of video, dan blijft het eraf.

Installeren:  pip3 install pychromecast
Hubs zoeken:  python3 lampen.py --zoek
Starten:      python3 lampen.py --hub "Woonkamer scherm" --api "<EXEC-URL>" --key "<SLEUTEL>"
"""
import argparse
import functools
import http.server
import os
import socket
import threading
import time
import urllib.parse

import pychromecast
from pychromecast.config import APP_DASHCAST
from pychromecast.controllers.dashcast import DashCastController

CONTROLE_ELKE = 60       # seconden tussen controles
OPNIEUW_ELKE = 6 * 3600  # eens per 6 uur vers casten
MAP = os.path.dirname(os.path.abspath(__file__))


def log(tekst):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), tekst, flush=True)


def zoek():
    casts, browser = pychromecast.get_chromecasts(timeout=10)
    for c in casts:
        print(f'  "{c.cast_info.friendly_name}"  ({c.cast_info.model_name}, {c.cast_info.host})')
    if not casts:
        print("  Niets gevonden. Zit dit apparaat op hetzelfde wifi als de Hub?")
    browser.stop_discovery()


def start_webserver(poort):
    class Stil(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

    handler = functools.partial(Stil, directory=MAP)
    server = http.server.ThreadingHTTPServer(("0.0.0.0", poort), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"Pagina beschikbaar op poort {poort}")


def eigen_ip(naar_host):
    """Het IP-adres van dit apparaat zoals de Hub het ziet."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((naar_host, 9))
        return s.getsockname()[0]
    finally:
        s.close()


def verbind(naam):
    while True:
        try:
            casts, browser = pychromecast.get_listed_chromecasts(friendly_names=[naam], discovery_timeout=15)
            browser.stop_discovery()
            if casts:
                cast = casts[0]
                cast.wait(timeout=20)
                dash = DashCastController()
                cast.register_handler(dash)
                log(f'Verbonden met "{naam}" ({cast.cast_info.host})')
                return cast, dash
            log(f'"{naam}" niet gevonden, over 30 s opnieuw (namen tonen: --zoek)')
        except Exception as e:  # netwerk weg, Hub herstart, ...
            log(f"Verbinden mislukt: {e}")
        time.sleep(30)


def main():
    a = argparse.ArgumentParser(description="Lampenpagina 24/7 op een Nest Hub")
    a.add_argument("--zoek", action="store_true", help="toon de Hubs op het netwerk")
    a.add_argument("--hub", help='naam van de Hub, bv. "Woonkamer scherm"')
    a.add_argument("--api", help="de /exec-URL van het Apps Script")
    a.add_argument("--key", help="de SLEUTEL uit het Apps Script")
    a.add_argument("--poort", type=int, default=8321)
    a.add_argument("--poll", type=int, default=120, help="seconden tussen statusupdates (standaard 120)")
    a.add_argument("--demo", action="store_true", help="toon voorbeeldgegevens, zonder koppeling")
    args = a.parse_args()

    if args.zoek:
        zoek()
        return
    if not args.hub or not (args.demo or (args.api and args.key)):
        a.error("geef --hub en --api en --key op (of --demo)")

    start_webserver(args.poort)
    cast, dash = verbind(args.hub)

    def url():
        q = {"demo": "1"} if args.demo else {"api": args.api, "key": args.key, "poll": args.poll}
        return f"http://{eigen_ip(cast.cast_info.host)}:{args.poort}/index.html?" + urllib.parse.urlencode(q)

    laatst = 0
    while True:
        try:
            if not cast.socket_client.is_connected:
                raise ConnectionError("verbinding met de Hub verbroken")
            # In rust (klok/fotolijst): pagina terugzetten.
            # Draait onze pagina al: alleen eens per 6 uur vers laden.
            # Iets anders (muziek, video): niet onderbreken.
            if cast.is_idle or (cast.app_id == APP_DASHCAST and time.time() - laatst > OPNIEUW_ELKE):
                log("Pagina casten")
                dash.load_url(url(), force=True, reload_seconds=0)
                laatst = time.time()
        except Exception as e:
            log(f"Fout: {e} — opnieuw verbinden")
            try:
                cast.disconnect(timeout=5)
            except Exception:
                pass
            cast, dash = verbind(args.hub)
            laatst = 0
        time.sleep(CONTROLE_ELKE)


if __name__ == "__main__":
    main()
