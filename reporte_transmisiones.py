#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reporte_transmisiones.py
========================
Vigila la pagina https://goluchitas.com (y su iframe de programacion en
tv.goluchitas.com), descubre todas las transmisiones en vivo, obtiene su
enlace HLS real y escribe un informe Markdown con la tabla resultante.

Uso:
    python3 reporte_transmisiones.py                     # informe en ./informe_transmisiones.md
    python3 reporte_transmisiones.py -o otro.md
    python3 reporte_transmisiones.py --scan 1-300         # ademas barre todos los canales
                                                          # tipo candyshop F1..F300
    python3 reporte_transmisiones.py --jobs 12

Requiere: python3 >= 3.8 y `requests` (pip install requests).
No necesita navegador: descifra los enlaces codificados de blib.shop (XOR)
y extrae las firmas candyshop.st desde siempreon.shop/fslivepro.php.
"""

import argparse
import base64
import concurrent.futures as cf
import datetime as dt
import json
import os
import re
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("Falta la libreria 'requests':  pip install requests")

# ---------------------------------------------------------------- constantes
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

HOME = "https://goluchitas.com/"
TV = "https://tv.goluchitas.com"
REST_PAGES = TV + "/wp-json/wp/v2/pages?per_page=100&_fields=title,link"

IGNORAR = ("gopelotero.com", "gochat.lol", "google.com/forms", "docs.google.com")
BLIB_KEY = "blib2024"          # clave XOR usada por los embeds de blib.shop

RE_PAT = re.compile(r'[^"\'\s\\<>)]*(?:candyshop|\.m3u8)[^"\'\s\\<>)]*', re.I)
IFRAME_PAT = re.compile(r'<iframe[^>]+?src=[\'"]([^\'"]+)[\'"]', re.I | re.S)
TITLE_PAT = re.compile(r'<title>([^<]*)', re.I)
STREAMURLS_PAT = re.compile(r'const streamUrls=\[(.*?)\];', re.S)
URL_PAT = re.compile(r'"(https?://[^"]+?)"')
ENCODED_PAT = re.compile(r'encoded_hls\s*=\s*"([^"]+)"')
KEY_PAT = re.compile(r'encode_key\s*=\s*"([^"]+)"')

_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"})


def get(url, ref=None, timeout=25):
    """Devuelve (status, texto). Nunca lanza: los fallos se anotan como 0."""
    h = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
    if ref:
        h["Referer"] = ref
    try:
        r = _session.get(url, headers=h, timeout=timeout, allow_redirects=True)
        return r.status_code, r.text
    except Exception:
        return 0, ""


# ------------------------------------------------------- descifrar blib.shop
def descifrar_blib(encoded, key=BLIB_KEY):
    """Reproduce decode_url() del embed: rotacion de base64 + reverse + XOR."""
    try:
        rot = ord(encoded[0]) - 65
        b64 = encoded[1:]
        orig = (b64[-rot:] + b64[:-rot]) if rot > 0 else b64
        datos = base64.b64decode(b64 if rot <= 0 else orig + "===").decode("latin1")
        rev = datos[::-1]
        return "".join(chr(ord(rev[i]) ^ ord(key[i % len(key)])) for i in range(len(rev)))
    except Exception:
        return ""


# ------------------------------------------------------------------ verificacion
def cabeceras_hls(url):
    """Cabeceras que exige cada CDN.

    - blib.shop      -> exige Referer + Origin propios o devuelve 403.
    - candyshop.st   -> al contrario, PROHIBE cualquier Referer (403 si va).
    """
    h = {"User-Agent": UA, "Accept": "*/*"}
    host = re.sub(r'^https?://', '', url).split('/')[0]
    if "blib.shop" in host:
        h.update({"Referer": "https://blib.shop/", "Origin": "https://blib.shop",
                  "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
                  "Sec-Fetch-Site": "cross-site"})
    return h


def verificar_hls(url, timeout=20):
    """GET del playlist: -> {estado, en_vivo, secuencia, nota}"""
    try:
        r = _session.get(url, headers=cabeceras_hls(url), timeout=timeout, stream=True)
    except Exception as e:
        return {"http": 0, "en_vivo": False, "secuencia": None, "nota": str(e)[:60]}
    if r.status_code != 200:
        return {"http": r.status_code, "en_vivo": False, "secuencia": None,
                "nota": {403: "firma rechazada o caducada", 404: "canal apagado"}
                        .get(r.status_code, "error")}
    try:
        texto = r.raw.read(6000, decode_content=True).decode("utf8", "ignore")
    except Exception:
        texto = ""
    if "#EXTM3U" not in texto:
        return {"http": 200, "en_vivo": False, "secuencia": None,
                "nota": "200 pero no es un playlist"}
    seq = re.search(r'#EXT-X-MEDIA-SEQUENCE:(\d+)', texto)
    es_vod = "#EXT-X-ENDLIST" in texto
    return {"http": 200, "en_vivo": not es_vod,
            "secuencia": int(seq.group(1)) if seq else None,
            "nota": "playlist VOD (fin de lista)" if es_vod else "playlist en directo"}


# --------------------------------------------------------------- descubrimiento
def iframes_de(html, base=""):
    out = []
    for s in dict.fromkeys(IFRAME_PAT.findall(html or "")):
        if s.startswith("//"):
            s = "https:" + s
        if not s.startswith("http"):
            continue
        if any(i in s for i in IGNORAR):
            continue
        out.append(s)
    return out


def resolver_embed(url, ref=None, hondo=0, cache=None):
    """Devuelve {'hls': [...], 'detalle': str} siguiendo los iframes anidados."""
    cache = cache if cache is not None else {}
    if url in cache:
        return cache[url]
    res = {"hls": [], "detalle": "", "embed": url}
    st, html = get(url, ref=ref)

    # --- blib.shop: el enlace viene codificado (XOR) dentro del embed
    if "blib.shop" in url and st == 200:
        m, k = ENCODED_PAT.search(html), KEY_PAT.search(html)
        if m and k:
            dec = descifrar_blib(m.group(1), k.group(1))
            if dec.startswith("http"):
                res["hls"] = [dec]
                res["detalle"] = "descifrado de blib.shop (clave %s)" % k.group(1)
                cache[url] = res
                return res
        res["detalle"] = "embed blib.shop sin enlace descifrable"
        cache[url] = res
        return res

    # --- siempreon.shop: array streamUrls[] con las firmas candyshop.st
    if "fslivepro.php" in url or "streamUrls" in html:
        m = STREAMURLS_PAT.search(html)
        if m:
            urls = [u.encode().decode("unicode_escape") for u in URL_PAT.findall(m.group(1))]
            if urls:
                res["hls"] = [urls[-1]] + [u for u in urls[:-1]]   # edge2 primero
                res["detalle"] = "firma candyshop.st (stream=%s)" % (
                    re.search(r'stream=([A-Za-z0-9]+)', url).group(1)
                    if re.search(r'stream=([A-Za-z0-9]+)', url) else "?")
                cache[url] = res
                return res

    # --- sigue bajando por iframes anidados
    if st == 200 and hondo < 3:
        for sub in iframes_de(html, url):
            r = resolver_embed(sub, ref=url, hondo=hondo + 1, cache=cache)
            if r["hls"]:
                res["hls"] = r["hls"]
                res["detalle"] = r["detalle"] or "reproductor embebido"
                res["embed"] = r["embed"]
                break
        if not res["hls"] and not res["detalle"]:
            res["detalle"] = "reproductor externo: %s (sin HLS directo)" % (
                iframes_de(html, url)[0] if iframes_de(html, url) else url)
    elif not res["detalle"]:
        res["detalle"] = "sin HLS (agotado el seguimiento de iframes)"

    cache[url] = res
    return res


def programacion_en_vivo():
    """Del iframe de programacion extrae: programa -> canales marcados EN VIVO."""
    st, html = get(TV + "/programacion/", ref=HOME)
    if st != 200:
        return [], []
    grupos = []
    for g in re.findall(r'<div class="schedule-group is-live">(.*?)(?=<div class="schedule-group|</div>\s*</div>\s*</div>)',
                        html, re.S):
        prog = re.search(r'<div class="name">([^<]+)', g)
        hora = re.search(r'<div class="cell time"[^>]*>([^<]+)', g)
        canales = []
        for a in re.findall(r'<a href="([^"]+)" class="option-row">(.*?)</a>', g, re.S):
            canales.append({"url": a[0].rstrip("/") + "/",
                            "nombre": (re.search(r'option-channel">([^<]+)', a[1]) or
                                       re.search(r'>([^<]+)<', a[1])).group(1).strip()})
        if canales:
            grupos.append({"programa": prog.group(1).strip() if prog else "—",
                           "hora": hora.group(1).strip() if hora else "—",
                           "canales": canales})
    enlaces = [c["url"] for g in grupos for c in g["canales"]]
    return grupos, enlaces


def paginas_del_sitio():
    st, body = get(REST_PAGES, ref=HOME)
    if st != 200:
        return []
    try:
        return json.loads(body)
    except Exception:
        return []


# -------------------------------------------------------------------- escaneo
def barre_candyshop(a, b, jobs=10):
    """Barre IDs Fxx en siempreon.shop y devuelve solo los que estan EN VIVO."""
    plantilla = "https://siempreon.shop/fslivepro.php?stream=%s"

    def probe(fid):
        st, html = get(plantilla % fid, ref="https://wikisport.info/")
        if st != 200:
            return None
        m = STREAMURLS_PAT.search(html)
        if not m:
            return None
        urls = [u.encode().decode("unicode_escape") for u in URL_PAT.findall(m.group(1))]
        if not urls:
            return None
        v = verificar_hls(urls[-1])
        if v["en_vivo"]:
            return {"id": fid, "url": urls[-1], "verif": v}
        return None

    ids = ["F%d" % i for i in range(a, b + 1)]
    vivos = []
    with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
        for r in ex.map(probe, ids):
            if r:
                vivos.append(r)
    return vivos


# --------------------------------------------------------------------- informe
def hora():
    utc = dt.datetime.now(dt.timezone.utc)
    hab = utc - dt.timedelta(hours=5)
    return utc.strftime("%Y-%m-%d %H:%M:%S UTC"), hab.strftime("%Y-%m-%d %H:%M:%S (Habana, UTC-5)")


def main():
    ap = argparse.ArgumentParser(description="Informe .md de transmisiones en vivo de goluchitas.com")
    ap.add_argument("-o", "--salida", default="informe_transmisiones.md", help="archivo .md de salida")
    ap.add_argument("--scan", metavar="A-B", help="barrido opcional de canales candyshop, p. ej. 1-300")
    ap.add_argument("--jobs", type=int, default=10, help="peticiones concurrentes")
    ap.add_argument("--json", dest="json_out", help="ademas vuelca los datos crudos en JSON")
    args = ap.parse_args()

    t0 = time.time()
    grupos, canales_en_vivo = programacion_en_vivo()
    paginas = paginas_del_sitio()
    print("[+] programacion: %d grupos en vivo | paginas del sitio: %d" % (len(grupos), len(paginas)), flush=True)

    # ---------- recoger todos los reproductores del sitio
    trabajo = []

    def traer_pagina(p):
        st, html = get(p["link"], ref=HOME)
        return (p["title"]["rendered"], p["link"], iframes_de(html)) if st == 200 else None

    with cf.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        for r in ex.map(traer_pagina, paginas):
            if r:
                for i in r[2]:
                    trabajo.append({"canal": r[0], "pagina": r[1], "embed": i})

    print("[+] reproductores encontrados: %d" % len(trabajo), flush=True)

    # ---------- resolver cada reproductor -> HLS
    cache = {}

    def traer_trabajo(t):
        r = resolver_embed(t["embed"], ref=t["pagina"], cache=cache)
        return {**t, **r}

    with cf.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        resueltos = list(ex.map(traer_trabajo, trabajo))

    # ---------- verificar los HLS
    unicos = {}
    for r in resueltos:
        for u in r.get("hls", []):
            unicos.setdefault(u, r)

    def ver(u):
        return u, verificar_hls(u)

    verifs = {}
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        for u, v in ex.map(ver, list(unicos)):
            verifs[u] = v
    print("[+] HLS unicos comprobados: %d" % len(verifs), flush=True)

    # ---------- barrido opcional de canales candyshop
    extra = []
    if args.scan:
        a, b = (int(x) for x in args.scan.split("-"))
        print("[+] barrido candyshop F%d..F%d ..." % (a, b), flush=True)
        extra = barre_candyshop(a, b, args.jobs)
        for e in extra:
            verifs.setdefault(e["url"], e["verif"])
        print("[+] canales candyshop en vivo: %d" % len(extra), flush=True)

    # ---------- montar tablas
    filas, sin_hls = [], []
    vistos = set()
    for r in resueltos:
        if not r.get("hls"):
            sin_hls.append(r)
            continue
        u = r["hls"][0]
        if u in vistos:
            continue
        vistos.add(u)
        v = verifs.get(u, {})
        programa = next((g["programa"] for g in grupos
                         for c in g["canales"] if c["url"].rstrip("/") == r["pagina"].rstrip("/")), "—")
        filas.append({"nombre": r["canal"], "programa": programa, "pagina": r["pagina"],
                      "hls": u, "v": v, "origen": "pagina goluchitas"})

    for e in extra:
        if e["url"] in vistos:
            continue
        vistos.add(e["url"])
        filas.append({"nombre": "Canal %s" % e["id"], "programa": "—", "pagina": "—",
                      "hls": e["url"], "v": verifs.get(e["url"], {}), "origen": "barido candyshop"})

    vivos = [f for f in filas if f["v"].get("en_vivo")]
    caidos = [f for f in filas if not f["v"].get("en_vivo")]

    # ---------- escribir el markdown
    utc, hab = hora()
    L = []
    L.append("# Transmisiones en vivo — goluchitas.com\n")
    L.append("**Fecha del informe:** %s · %s\n" % (utc, hab))
    L.append("**Pagina vigilada:** %s (iframe de programacion: %s/programacion/)\n" % (HOME, TV))
    L.append("**Tiempo de ejecucion:** %.1f s\n" % (time.time() - t0))
    L.append("\n## Resumen\n")
    L.append("| Concepto | Cantidad |")
    L.append("|---|---:|")
    L.append("| Marcados como EN VIVO en la programacion | %d |" % sum(len(g["canales"]) for g in grupos))
    L.append("| Reproductores analizados en el sitio | %d |" % len(resueltos))
    L.append("| Enlaces HLS distintos obtenidos | %d |" % len(filas))
    L.append("| **Transmisiones EN VIVO verificadas** | **%d** |" % len(vivos))
    L.append("| HLS que no responden (apagados / firma caducada) | %d |" % len(caidos))
    L.append("| Reproductores sin HLS directo | %d |" % len(sin_hls))

    L.append("\n## Tabla de transmisiones y sus HLS\n")
    L.append("| # | Transmision | Programa | Enlace HLS | Estado |")
    L.append("|---:|---|---|---|---|")
    for i, f in enumerate(sorted(filas, key=lambda x: (not x["v"].get("en_vivo"), x["nombre"])), 1):
        v = f["v"]
        if v.get("en_vivo"):
            estado = "EN VIVO (HTTP %s, secuencia %s)" % (v.get("http"), v.get("secuencia"))
        else:
            estado = "— %s (HTTP %s)" % (v.get("nota") or "sin comprobar", v.get("http"))
        L.append("| %d | **%s** | %s | <%s> | %s |"
                 % (i, f["nombre"], f["programa"], f["hls"], estado))

    L.append("\n### Estado detallado\n")
    L.append("| Transmision | Origen | HTTP | Secuencia | Comentario |")
    L.append("|---|---|---:|---:|---|")
    for f in sorted(filas, key=lambda x: (not x["v"].get("en_vivo"), x["nombre"])):
        v = f["v"]
        L.append("| %s | %s | %s | %s | %s |" % (
            f["nombre"], f["origen"], v.get("http", "—"),
            v.get("secuencia") if v.get("secuencia") is not None else "—",
            v.get("nota") or "—"))

    if sin_hls:
        L.append("\n## Reproductores sin enlace HLS directo\n")
        L.append("| Transmision | Pagina | Reproductor | Motivo |")
        L.append("|---|---|---|---|")
        for r in sin_hls:
            L.append("| %s | %s | %s | %s |" % (
                r["canal"], r["pagina"], r.get("embed", "—"), (r.get("detalle") or "")[:90]))

    L.append("\n## Notas para el reporte\n")
    L.append("- Las firmas `st=` de candyshop.st caducan en la fecha `e=` del propio enlace "
             "(unas 3 horas). Vuelve a ejecutar el script para regenerarlas.")
    L.append("- Las URLs de `blib.shop` se descifran en el cliente (rotacion base64 + XOR con "
             "clave `%s`); no aparecen en claro en el HTML." % BLIB_KEY)
    L.append("- Un canal se da por EN VIVO solo si el servidor responde `HTTP 200` con "
             "`#EXTM3U` y **sin** `#EXT-X-ENDLIST` (playlist de directo).")
    L.append("- El mismo enlace funciona en `candyshop.st`, `edge`, `edge1` y `edge2` "
             "(misma firma para los cuatro).")
    L.append("\n---\n_Generado automaticamente por `reporte_transmisiones.py`._\n")

    with open(args.salida, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    print("[+] informe escrito en %s  (transmisiones en vivo: %d)"
          % (args.salida, len(vivos)), flush=True)

    if args.json_out:
        json.dump({"fecha_utc": utc, "grupos": grupos, "transmisiones": filas},
                  open(args.json_out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
