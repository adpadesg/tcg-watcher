#!/usr/bin/env python3
"""
TCG Watcher
-----------
Vigila categorías de tiendas de cartas y avisa por Telegram de dos cosas:
un producto nuevo disponible, o un producto que se ha quedado sin stock.

La unidad que se vigila es **producto + idioma**: en algunas tiendas el mismo
producto está disponible en japonés y agotado en inglés, y son dos cosas
distintas a efectos de compra.

    python3 scraper.py                  # una ronda de vigilancia
    python3 scraper.py --listar         # qué se vigila y dónde se configura
    python3 scraper.py --check URL      # analiza una tienda antes de añadirla
    python3 scraper.py --seed           # guarda el estado actual sin avisar
    python3 scraper.py --informe        # resumen diario (solo a las 20:00 CET)
    python3 scraper.py --comandos       # atiende los comandos de Telegram
"""

import argparse
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
import html
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = 20
DELAY_BETWEEN_URLS = (2, 5)   # segundos, con jitter, para no machacar la web
ZONA_INFORME = "Europe/Madrid"
HORA_INFORME = 20

# Selectores por defecto: WooCommerce estándar. Cada fuente puede sobreescribir
# los que necesite, para tiendas con otra plantilla.
DEFAULT_SELECTORS = {
    "container": "li.product, .products .product, .product-grid-item",
    # WooCommerce marca las subcategorías del catálogo con la clase "product",
    # pero no son productos y no deben notificarse.
    "skip_classes": ["product-category", "product-cat"],
    "link": [
        "h2 a", "h3 a", ".woocommerce-loop-product__title a",
        "a.woocommerce-LoopProduct-link", "a.wd-product-img-link",
        "a[href*='/producto/']", "a[href*='/product/']", "a[href]",
    ],
    "title": [
        ".wd-entities-title", ".woocommerce-loop-product__title",
        ".product-title", "h2", "h3",
    ],
    "price": [".price", ".product-price", ".amount"],
    "pagination": ".woocommerce-pagination a, nav.pagination a, .page-numbers a",
    # Tabla de atributos de la ficha, de donde se saca el idioma
    "atributos": ".woocommerce-product-attributes, .shop_attributes",
}

BANDERAS = {
    "Español": "🇪🇸", "Inglés": "🇬🇧", "Japonés": "🇯🇵", "Chino": "🇨🇳",
    "Coreano": "🇰🇷", "Francés": "🇫🇷", "Alemán": "🇩🇪", "Italiano": "🇮🇹",
}
BANDERA_DESCONOCIDA = "❔"
EMOJI_CATEGORIA_POR_DEFECTO = "🃏"

IDIOMAS_CONOCIDOS = {
    "español": "Español", "castellano": "Español", "esp": "Español", "spa": "Español",
    "inglés": "Inglés", "ingles": "Inglés", "eng": "Inglés", "en": "Inglés",
    "japonés": "Japonés", "japones": "Japonés", "jap": "Japonés", "jpn": "Japonés",
    "chino": "Chino", "chn": "Chino",
    "coreano": "Coreano", "kor": "Coreano",
    "francés": "Francés", "frances": "Francés",
    "alemán": "Alemán", "aleman": "Alemán",
    "italiano": "Italiano",
}


# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #

def load_dotenv(path: str = ".env"):
    """Carga variables KEY=VALUE de un .env local, si existe.

    Así el token no vive en fuentes.json, que sí se sube a GitHub. En Actions
    las variables vienen de los secrets del repo y este fichero no existe.
    """
    env_file = Path(path)
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        clave, _, valor = line.partition("=")
        os.environ.setdefault(clave.strip(), valor.strip().strip('"').strip("'"))


def expand_env(valor):
    """Sustituye ${VARIABLE} por su valor en el entorno."""
    if not isinstance(valor, str):
        return valor

    def repl(match):
        nombre = match.group(1)
        resuelto = os.environ.get(nombre)
        # Una variable vacía se trata como ausente: si un secret no existe,
        # GitHub Actions no da error, pone la variable a cadena vacía. Sin
        # esta comprobación el token vacío llegaría hasta la API de Telegram
        # y el fallo aparecería mucho más tarde y sin explicación.
        if not resuelto:
            raise ValueError(
                f"La variable de entorno '{nombre}' no está definida o está vacía. "
                "Defínela en .env (local) o como secret del repositorio "
                "(Settings > Secrets and variables > Actions)."
            )
        return resuelto

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", repl, valor)


def slug(texto: str) -> str:
    """Convierte "Pokémon" en "pokemon", para nombrar ficheros sin sorpresas."""
    sin_tildes = unicodedata.normalize("NFKD", texto)
    sin_tildes = "".join(c for c in sin_tildes if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "_", sin_tildes.lower()).strip("_")


def derivar_tienda(url: str, nombres: dict) -> str:
    """Deduce el nombre de la tienda a partir del dominio.

    De 'flashstore.es' no hay forma fiable de sacar "Flash Store" —partir la
    palabra requeriría un diccionario—, así que se devuelve 'Flashstore' y el
    mapa 'nombres_tienda' permite corregirlo para todas sus URLs.
    """
    dominio = urlparse(url).netloc.split(":")[0].lower()
    if dominio.startswith("www."):
        dominio = dominio[4:]
    if dominio in nombres:
        return nombres[dominio]

    # Dominios de dos niveles (.co.uk, .com.es...): quedarse con la penúltima
    # parte daría tiendas llamadas "Co".
    SUFIJOS_COMPUESTOS = {"co", "com", "org", "net", "gov", "edu", "ac", "nom"}
    partes = dominio.split(".")
    if len(partes) >= 3 and partes[-2] in SUFIJOS_COMPUESTOS:
        etiqueta = partes[-3]
    elif len(partes) >= 2:
        etiqueta = partes[-2]
    else:
        etiqueta = dominio
    if etiqueta in nombres:
        return nombres[etiqueta]
    return etiqueta.replace("-", " ").replace("_", " ").title()


def es_shopify(url: str) -> bool:
    return "/collections/" in urlparse(url).path


def load_fuentes(path: str) -> Tuple[dict, List[dict]]:
    """Lee fuentes.json y devuelve (telegram, lista de fuentes).

    El fichero es una lista plana de URLs: cada una declara a qué categoría
    pertenece y, opcionalmente, a qué tienda. No se impone jerarquía
    tienda→categoría porque no existe y cambia con el tiempo.
    """
    load_dotenv()

    with open(path, "r", encoding="utf-8") as f:
        raiz = json.load(f)

    if "fuentes" not in raiz:
        raise ValueError(f"Falta la lista 'fuentes' en {path}")
    if "telegram" not in raiz:
        raise ValueError(f"Falta la sección 'telegram' en {path}")
    if "bot_token" not in raiz["telegram"] or "chat_id" not in raiz["telegram"]:
        raise ValueError("La sección 'telegram' necesita 'bot_token' y 'chat_id'")

    telegram = {k: expand_env(v) for k, v in raiz["telegram"].items()}
    nombres = {k.lower(): v for k, v in raiz.get("nombres_tienda", {}).items()}
    # Cada categoría lleva su emoji para que los resúmenes con varias
    # categorías se distingan de un vistazo.
    emojis = {slug(k): v for k, v in raiz.get("emojis_categoria", {}).items()}

    selectores_base = dict(DEFAULT_SELECTORS)
    selectores_base.update(raiz.get("selectors", {}))
    max_pages_base = raiz.get("max_pages", 20)

    fuentes = []
    urls_vistas = {}
    for i, entrada in enumerate(raiz["fuentes"], start=1):
        if not isinstance(entrada, dict):
            raise ValueError(
                f"La fuente #{i} de {path} debe ser un objeto "
                '{"url": "...", "categoria": "..."}, no ' + repr(entrada)
            )
        url = entrada.get("url")
        categoria = entrada.get("categoria")
        if not url or not categoria:
            raise ValueError(
                f'La fuente #{i} de {path} necesita "url" y "categoria". Tiene: {entrada!r}'
            )
        if not str(url).startswith(("http://", "https://")):
            raise ValueError(f"La fuente #{i} de {path} no es una URL válida: {url!r}")
        if url in urls_vistas:
            raise ValueError(
                f"La URL {url} está repetida en {path} "
                f"(fuentes #{urls_vistas[url]} y #{i})."
            )
        urls_vistas[url] = i

        tipo = entrada.get("tipo", "auto")
        if tipo not in ("auto", "html", "shopify"):
            raise ValueError(
                f'La fuente #{i} tiene un "tipo" desconocido: {tipo!r}. '
                'Usa "auto", "html" o "shopify".'
            )
        if tipo == "auto":
            tipo = "shopify" if es_shopify(url) else "html"

        selectores = dict(selectores_base)
        selectores.update(entrada.get("selectors", {}))

        fuentes.append({
            "url": url,
            "categoria": categoria,
            "emoji": emojis.get(slug(categoria), EMOJI_CATEGORIA_POR_DEFECTO),
            "tipo": tipo,
            "tienda": entrada.get("tienda") or derivar_tienda(url, nombres),
            "selectors": selectores,
            "max_pages": entrada.get("max_pages", max_pages_base),
            "db_path": raiz.get("db_path", "estado.db"),
            "horas_latido": raiz.get("horas_latido", HORAS_LATIDO_POR_DEFECTO),
        })

    if not fuentes:
        raise ValueError(f"La lista 'fuentes' de {path} está vacía.")
    return telegram, fuentes


class CapturaErrores(logging.Handler):
    """Guarda los errores del log para poder avisarlos por Telegram.

    Un vigilante que se rompe en silencio es inútil: si deja de avisar, el
    silencio es indistinguible de "no hay novedades". Estos mensajes son los
    que se mandan al chat cuando algo va mal.
    """

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.mensajes: List[str] = []

    def emit(self, record):
        try:
            self.mensajes.append(record.getMessage())
        except Exception:
            pass


INCIDENCIAS = CapturaErrores()


def setup_logging(log_path: str = "tcg_watcher.log"):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
            INCIDENCIAS,
        ],
    )


# --------------------------------------------------------------------------- #
# Base de datos
# --------------------------------------------------------------------------- #

ESQUEMA = """
    -- Un artículo = producto + idioma. No se guardan imágenes: solo texto,
    -- para que la base sea pequeña y las consultas rápidas.
    CREATE TABLE IF NOT EXISTS articulos (
        clave        TEXT PRIMARY KEY,
        fuente_url   TEXT NOT NULL,
        tienda       TEXT NOT NULL,
        categoria    TEXT NOT NULL,
        product_url  TEXT NOT NULL,
        titulo       TEXT NOT NULL,
        precio       TEXT,
        idioma       TEXT,
        en_stock     INTEGER NOT NULL DEFAULT 0,
        primera_vez  TEXT NOT NULL,
        ultimo_visto TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS eventos (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        fecha       TEXT NOT NULL,
        tipo        TEXT NOT NULL,
        tienda      TEXT NOT NULL,
        categoria   TEXT NOT NULL,
        titulo      TEXT NOT NULL,
        precio      TEXT,
        idioma      TEXT,
        product_url TEXT
    );

    CREATE TABLE IF NOT EXISTS estado (
        clave TEXT PRIMARY KEY,
        valor TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_eventos_fecha ON eventos(fecha);
    CREATE INDEX IF NOT EXISTS idx_articulos_fuente ON articulos(fuente_url);
"""

DIAS_DE_HISTORIAL = 90


def ruta_volcado(db_path: str) -> Path:
    return Path(db_path).with_suffix(".sql")


def init_db(db_path: str) -> sqlite3.Connection:
    """Abre la base y, si no existe, la reconstruye desde el volcado de texto.

    En el repositorio se versiona un volcado SQL en texto, no el fichero
    binario: git guarda solo las líneas que cambian, mientras que de un SQLite
    guardaría el fichero entero (300 KB) en cada commit. Cada ejecución en la
    nube empieza sin base, así que la reconstruye a partir del volcado.
    """
    volcado = ruta_volcado(db_path)
    hay_que_reconstruir = not Path(db_path).is_file() and volcado.is_file()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if hay_que_reconstruir:
        conn.executescript(volcado.read_text(encoding="utf-8"))
        logging.info("Estado reconstruido desde %s", volcado)
    conn.executescript(ESQUEMA)
    conn.commit()
    return conn


def guardar_volcado(conn, db_path: str):
    """Escribe el volcado de texto que se versiona en el repositorio."""
    limite = (datetime.now(timezone.utc) - timedelta(days=DIAS_DE_HISTORIAL)).isoformat()
    conn.execute("DELETE FROM eventos WHERE fecha < ?", (limite,))
    conn.commit()
    texto = "\n".join(conn.iterdump())
    ruta_volcado(db_path).write_text(texto + "\n", encoding="utf-8")


def ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def leer_estado(conn, clave: str) -> Optional[str]:
    fila = conn.execute("SELECT valor FROM estado WHERE clave = ?", (clave,)).fetchone()
    return fila["valor"] if fila else None


def escribir_estado(conn, clave: str, valor: str):
    conn.execute(
        "INSERT INTO estado (clave, valor) VALUES (?, ?) "
        "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor",
        (clave, str(valor)),
    )
    conn.commit()


def registrar_evento(conn, tipo: str, art: dict):
    conn.execute(
        "INSERT INTO eventos (fecha, tipo, tienda, categoria, titulo, precio, idioma, product_url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ahora(), tipo, art["tienda"], art["categoria"], art["titulo"],
         art.get("precio"), art.get("idioma"), art.get("product_url")),
    )


# --------------------------------------------------------------------------- #
# Descarga
# --------------------------------------------------------------------------- #

def fetch_html(url: str) -> str:
    resp = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "es-ES,es;q=0.9"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.text


def clean_price(raw: str) -> str:
    if not raw:
        return ""
    raw = raw.replace("\xa0", " ").strip()
    # Con precio tachado + oferta, quedarse con el último importe (el de oferta)
    precios = re.findall(r"[\d.,]+\s*€", raw)
    return precios[-1].strip() if precios else raw


def normalizar_idioma(texto: Optional[str]) -> Optional[str]:
    """Reconoce el idioma en un texto libre ('Sobre Inglés' -> 'Inglés')."""
    if not texto:
        return None
    limpio = texto.strip()
    directo = IDIOMAS_CONOCIDOS.get(limpio.lower())
    if directo:
        return directo
    for palabra in re.findall(r"[A-Za-zÁÉÍÓÚáéíóúÑñ]+", limpio):
        encontrado = IDIOMAS_CONOCIDOS.get(palabra.lower())
        if encontrado:
            return encontrado
    return None


# --------------------------------------------------------------------------- #
# Tiendas en HTML (WooCommerce y similares)
# --------------------------------------------------------------------------- #

def _first_match(container, selectores: List[str]):
    """Primer elemento que encuentre alguno de los selectores, por prioridad."""
    for selector in selectores:
        try:
            hallado = container.select_one(selector)
        except Exception:
            logging.warning("Selector inválido, se ignora: %s", selector)
            continue
        if hallado:
            return hallado
    return None


def parse_products(html: str, source_url: str, selectors: Optional[dict] = None) -> List[dict]:
    selectors = selectors or DEFAULT_SELECTORS
    soup = BeautifulSoup(html, "html.parser")
    productos = []
    vistos = set()
    saltar = set(selectors.get("skip_classes", []))

    for contenedor in soup.select(selectors["container"]):
        if saltar.intersection(contenedor.get("class") or []):
            continue

        enlace = _first_match(contenedor, selectors["link"])
        if not enlace or not enlace.get("href"):
            continue

        product_url = urljoin(source_url, enlace["href"])
        if product_url in vistos:
            continue

        titulo = enlace.get_text(strip=True)
        if not titulo:
            el = _first_match(contenedor, selectors["title"])
            if el:
                titulo = el.get_text(strip=True)
        if not titulo:
            titulo = enlace.get("aria-label") or "(sin título)"

        el_precio = _first_match(contenedor, selectors["price"])
        precio = clean_price(el_precio.get_text(" ", strip=True)) if el_precio else ""

        vistos.add(product_url)
        productos.append({"product_url": product_url, "titulo": titulo, "precio": precio})

    return productos


def find_page_urls(html: str, source_url: str, max_pages: int,
                   pagination_selector: str = DEFAULT_SELECTORS["pagination"]) -> List[str]:
    """URLs de las páginas 2..N, conservando la query de la original.

    Perder la query al paginar traería productos que el filtro de stock
    excluía, y se notificarían como novedades.
    """
    if max_pages <= 1:
        return []

    soup = BeautifulSoup(html, "html.parser")
    ultima = 1
    for a in soup.select(pagination_selector):
        texto = a.get_text(strip=True)
        if texto.isdigit():
            ultima = max(ultima, int(texto))
        m = re.search(r"/page/(\d+)/?", a.get("href") or "")
        if m:
            ultima = max(ultima, int(m.group(1)))

    tope = min(ultima, max_pages)
    partes = urlparse(source_url)
    ruta = re.sub(r"/page/\d+$", "", partes.path.rstrip("/"))
    cola = f"?{partes.query}" if partes.query else ""
    base = f"{partes.scheme}://{partes.netloc}{ruta}"
    return [f"{base}/page/{n}/{cola}" for n in range(2, tope + 1)], ultima > max_pages


def idioma_de_ficha(product_url: str, selectors: dict) -> Optional[str]:
    """Lee el idioma de la ficha del producto (tabla de atributos).

    Solo se consulta al descubrir un producto nuevo: el idioma no cambia, así
    que se guarda en la base y no se vuelve a pedir en cada ronda.
    """
    try:
        html = fetch_html(product_url)
    except requests.RequestException as e:
        logging.warning("No se pudo abrir la ficha %s: %s", product_url, e)
        return None

    soup = BeautifulSoup(html, "html.parser")
    tabla = soup.select_one(selectors.get("atributos", ""))
    if tabla:
        for fila in tabla.select("tr"):
            th, td = fila.select_one("th"), fila.select_one("td")
            if th and td and "idioma" in th.get_text(strip=True).lower():
                return normalizar_idioma(td.get_text(" ", strip=True)) or td.get_text(strip=True)
    return None


def articulos_html(fuente: dict, conn) -> Tuple[List[dict], bool, int]:
    """Artículos en stock de una categoría en HTML.

    La URL ya viene filtrada por stock, así que todo lo que aparece está
    disponible. Devuelve (artículos, listado_completo, errores): si la
    paginación se corta por el tope, el listado NO está completo y no se puede
    deducir que lo ausente esté agotado.
    """
    url, selectores = fuente["url"], fuente["selectors"]
    errores = 0

    try:
        html = fetch_html(url)
    except requests.RequestException as e:
        logging.error("No se pudo descargar %s: %s", url, e)
        return [], False, 1

    siguientes, truncado = find_page_urls(html, url, fuente["max_pages"], selectores["pagination"])
    paginas = [(url, html)]
    for page_url in siguientes:
        time.sleep(random.uniform(*DELAY_BETWEEN_URLS))
        try:
            paginas.append((page_url, fetch_html(page_url)))
        except requests.RequestException as e:
            logging.error("No se pudo descargar %s: %s", page_url, e)
            errores += 1
            truncado = True

    articulos, vistos = [], set()
    for page_url, page_html in paginas:
        encontrados = parse_products(page_html, page_url, selectores)
        logging.info("%s -> %d productos", page_url, len(encontrados))
        if not encontrados:
            logging.error(
                "0 productos en %s: puede que la tienda haya cambiado su HTML.", page_url
            )
            errores += 1
            truncado = True
        for prod in encontrados:
            if prod["product_url"] in vistos:
                continue
            vistos.add(prod["product_url"])
            articulos.append({
                "clave": prod["product_url"],
                "fuente_url": url,
                "tienda": fuente["tienda"],
                "categoria": fuente["categoria"],
                "product_url": prod["product_url"],
                "titulo": prod["titulo"],
                "precio": prod["precio"],
                "idioma": None,      # se completa desde la ficha si es nuevo
                "en_stock": 1,
            })

    if truncado:
        logging.warning(
            "El listado de %s no está completo: esta ronda no marcará agotados.", url
        )
    return articulos, not truncado, errores


# --------------------------------------------------------------------------- #
# Tiendas Shopify (API JSON)
# --------------------------------------------------------------------------- #

SHOPIFY_POR_PAGINA = 250
SHOPIFY_MAX_PAGINAS = 20


def _formato_euros(valor: float) -> str:
    """4900.0 -> '4.900,00 €'"""
    return f"{valor:,.2f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".") + " €"


def _idioma_de_variante(producto: dict, variante: dict) -> Optional[str]:
    """Idioma de una variante de Shopify.

    Si el producto tiene una opción llamada "Idioma" se usa esa posición
    exacta; si no, se busca un idioma reconocible en el título de la variante
    (que a veces es "Sobre Inglés" o "Caja Japonés").
    """
    for i, opcion in enumerate(producto.get("options", []), start=1):
        if slug(opcion.get("name", "")) == "idioma":
            return normalizar_idioma(variante.get(f"option{i}")) or variante.get(f"option{i}")
    return normalizar_idioma(variante.get("title"))


def articulos_shopify(fuente: dict) -> Tuple[List[dict], bool, int]:
    """Artículos de una colección de Shopify, uno por variante.

    Se usa products.json en vez del HTML porque muchos temas pintan la parrilla
    con JavaScript y el HTML llega sin el listado. La API es parte de Shopify,
    no del tema, así que no se rompe al cambiar de diseño, y trae la
    disponibilidad de cada variante — que es justo lo que interesa: un producto
    puede estar agotado en inglés y disponible en japonés.
    """
    partes = urlparse(fuente["url"])
    trozos = [t for t in partes.path.split("/") if t]
    try:
        handle = trozos[trozos.index("collections") + 1]
    except (ValueError, IndexError):
        logging.error("No se pudo sacar la colección de %s", fuente["url"])
        return [], False, 1

    origen = f"{partes.scheme}://{partes.netloc}"
    api = f"{origen}/collections/{handle}/products.json"

    articulos, errores, completo = [], 0, True
    for pagina in range(1, SHOPIFY_MAX_PAGINAS + 1):
        try:
            resp = requests.get(
                api,
                params={"limit": SHOPIFY_POR_PAGINA, "page": pagina},
                headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            lote = resp.json().get("products", [])
        except (requests.RequestException, ValueError) as e:
            logging.error("Fallo leyendo %s (página %d): %s", api, pagina, e)
            return articulos, False, errores + 1

        for prod in lote:
            for var in prod.get("variants", []):
                try:
                    precio = _formato_euros(float(var["price"]))
                except (KeyError, TypeError, ValueError):
                    precio = ""
                articulos.append({
                    "clave": f"{origen}/products/{prod['handle']}#{var['id']}",
                    "fuente_url": fuente["url"],
                    "tienda": fuente["tienda"],
                    "categoria": fuente["categoria"],
                    "product_url": f"{origen}/products/{prod['handle']}?variant={var['id']}",
                    "titulo": prod.get("title") or "(sin título)",
                    "precio": precio,
                    "idioma": _idioma_de_variante(prod, var),
                    "en_stock": 1 if var.get("available") else 0,
                })

        if len(lote) < SHOPIFY_POR_PAGINA:
            break
        time.sleep(random.uniform(*DELAY_BETWEEN_URLS))
    else:
        completo = False
        logging.warning("Se alcanzó el tope de páginas en %s", api)

    en_stock = sum(a["en_stock"] for a in articulos)
    logging.info("%s -> %d artículos (%d en stock)", api, len(articulos), en_stock)
    if not articulos:
        logging.error("0 artículos en %s: ¿ha cambiado la colección?", api)
        errores += 1
        completo = False

    return articulos, completo, errores


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

LIMITE_TELEGRAM = 3900   # el máximo real son 4096; se deja margen
PAUSA_ENTRE_MENSAJES = 1.1   # segundos: límite de Telegram hacia un mismo chat
SILENCIO = False   # con --sin-avisos: se registra lo que se enviaría, sin enviarlo


def bandera(idioma: Optional[str]) -> str:
    return BANDERAS.get(idioma or "", BANDERA_DESCONOCIDA)


def esc(texto: Optional[str]) -> str:
    """Escapa el texto para el modo HTML de Telegram."""
    return html.escape(texto or "", quote=False)


def enlace(url: Optional[str], texto: str = "Pincha aquí") -> str:
    """Enlace corto, para que el mensaje no se llene de URLs largas."""
    if not url:
        return ""
    return f'🔗 <a href="{html.escape(url, quote=True)}">{esc(texto)}</a>' 


def enviar_mensaje(telegram: dict, texto: str, chat_id: Optional[str] = None) -> bool:
    """Envía un mensaje en modo HTML, troceándolo si excede el límite.

    El corte se hace siempre entre líneas: partir por la mitad dejaría una
    etiqueta <a> abierta y Telegram rechazaría el mensaje entero.
    """
    if SILENCIO:
        logging.info("[sin avisos] no se envía:\n%s", texto[:400])
        return True

    destino = chat_id or telegram["chat_id"]
    api = f"https://api.telegram.org/bot{telegram['bot_token']}/sendMessage"

    trozos, actual = [], ""
    for linea in texto.split("\n"):
        if len(actual) + len(linea) + 1 > LIMITE_TELEGRAM:
            trozos.append(actual)
            actual = ""
        actual += linea + "\n"
    if actual.strip():
        trozos.append(actual)

    for trozo in trozos:
        if not _enviar_trozo(api, destino, trozo):
            return False
        # Telegram admite en torno a un mensaje por segundo hacia un mismo
        # chat. Como ahora se manda un aviso por producto, una tanda de altas
        # se pasaría del límite y se perderían avisos.
        time.sleep(PAUSA_ENTRE_MENSAJES)
    return True


def _enviar_trozo(api: str, destino: str, texto: str, reintento: bool = True) -> bool:
    try:
        r = requests.post(
            api,
            data={"chat_id": destino, "text": texto, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        logging.error("Excepción enviando a Telegram: %s", type(e).__name__)
        return False

    if r.status_code == 200:
        return True

    # 429 = demasiados mensajes seguidos. Telegram dice cuánto hay que esperar:
    # se espera y se reintenta una vez, en vez de dar el aviso por perdido.
    if r.status_code == 429 and reintento:
        try:
            espera = int(r.json().get("parameters", {}).get("retry_after", 3))
        except ValueError:
            espera = 3
        logging.warning("Telegram pide esperar %d s antes de reenviar.", espera)
        time.sleep(min(espera, 60) + 1)
        return _enviar_trozo(api, destino, texto, reintento=False)

    detalle = ""
    try:
        detalle = r.json().get("description", "")
    except ValueError:
        pass
    logging.error("Telegram devolvió HTTP %s: %s", r.status_code, detalle)
    return False



# Título del aviso y etiqueta para el resumen diario, por tipo de cambio.
ESTADOS = {
    "nuevo":    ("Se ha añadido un nuevo producto a", None),
    "repuesto": ("Vuelve a estar disponible un producto de", "Volvió a estar disponible:"),
    "agotado":  ("Se ha agotado un producto de", "Se agotó:"),
}


def bloque_producto(art) -> str:
    """Estructura estándar de un producto, en tres líneas.

    Es la misma en todas partes —altas, bajas, resumen diario y listado—, para
    que el ojo reconozca el patrón sin leerlo entero:

        🇯🇵 Nombre del producto
        24,99 €
        🔗 Pincha aquí
    """
    datos = dict(art)
    lineas = [f"{bandera(datos.get('idioma'))} <b>{esc(datos['titulo'])}</b>",
              esc(datos.get("precio") or "Precio no disponible")]
    destino = enlace(datos.get("product_url"))
    if destino:
        lineas.append(destino)
    return "\n".join(lineas)


def avisar_cambio(telegram: dict, tipo: str, tienda: str, art: dict) -> bool:
    """Un mensaje por producto.

    Nunca se agrupan varios productos en un mismo mensaje, ni cuando la misma
    ronda detecta varios cambios: cada aviso debe poder leerse y actuarse por
    separado desde la notificación del móvil.
    """
    titulo, _ = ESTADOS[tipo]
    return enviar_mensaje(
        telegram, f"{titulo} <b>{esc(tienda)}</b>\n\n{bloque_producto(art)}"
    )


# --------------------------------------------------------------------------- #
# Salud del servicio
# --------------------------------------------------------------------------- #

HORAS_LATIDO_POR_DEFECTO = 12
MINUTOS_ENTRE_AVISOS_DE_ERROR = 60


def _hace_mas_de(conn, clave: str, minutos: int) -> bool:
    """¿Ha pasado ese tiempo desde la última vez que se marcó esa clave?"""
    anterior = leer_estado(conn, clave)
    if not anterior:
        return True
    try:
        momento = datetime.fromisoformat(anterior)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - momento >= timedelta(minutes=minutos)


def avisar_incidencias(telegram: dict, conn, errores: int) -> None:
    """Avisa por Telegram de que algo va mal, y de cuándo se ha recuperado.

    Se limita a un aviso por hora: si una tienda deja de responder, sin ese
    freno llegarían 48 mensajes al día repitiendo lo mismo.
    """
    hubo_antes = leer_estado(conn, "hubo_errores") == "1"

    if errores:
        escribir_estado(conn, "hubo_errores", "1")
        if not _hace_mas_de(conn, "ultimo_aviso_error", MINUTOS_ENTRE_AVISOS_DE_ERROR):
            logging.info("Ya se avisó de un fallo hace menos de una hora; no se repite.")
            return
        detalle = "\n".join(f"· {esc(m[:200])}" for m in INCIDENCIAS.mensajes[:5])
        extra = "" if len(INCIDENCIAS.mensajes) <= 5 else \
            f"\n\n(y {len(INCIDENCIAS.mensajes) - 5} más)"
        enviar_mensaje(
            telegram,
            "⚠️ <b>Problema en el vigilante</b>\n\n"
            f"La última ronda terminó con {errores} error(es):\n\n{detalle}{extra}"
            "\n\nRevisa los registros del repositorio."
        )
        escribir_estado(conn, "ultimo_aviso_error", ahora())
        logging.info("Enviado el aviso de fallo por Telegram.")
        return

    escribir_estado(conn, "hubo_errores", "0")
    if hubo_antes:
        enviar_mensaje(telegram, "✅ <b>Todo vuelve a funcionar</b>\n\n"
                                 "La última ronda ha terminado sin errores.")
        logging.info("Enviado el aviso de recuperación.")
        escribir_estado(conn, "ultimo_aviso_error", "")


def latido(telegram: dict, conn, horas: int) -> None:
    """Mensaje periódico de "sigo vivo".

    Sin él, un scraper parado se parece demasiado a un scraper sin novedades.
    """
    if not _hace_mas_de(conn, "ultimo_latido", horas * 60):
        return

    filas = conn.execute(
        "SELECT categoria, tienda, SUM(en_stock) s, COUNT(*) t"
        " FROM articulos GROUP BY categoria, tienda ORDER BY categoria, tienda"
    ).fetchall()
    lineas = ["💚 <b>El vigilante sigue en marcha</b>", ""]
    for f in filas:
        lineas.append(f"{esc(f['categoria'])} — {esc(f['tienda'])}: "
                      f"{f['s'] or 0} en stock de {f['t']}")
    desde = leer_estado(conn, "ultimo_latido")
    lineas.append("")
    lineas.append(f"Sin novedades que avisar desde el último aviso."
                  if desde else "Primer aviso de estado.")

    if enviar_mensaje(telegram, "\n".join(lineas)):
        escribir_estado(conn, "ultimo_latido", ahora())
        logging.info("Enviado el aviso de estado del servicio.")


# --------------------------------------------------------------------------- #
# Ronda de vigilancia
# --------------------------------------------------------------------------- #

def run(telegram: dict, fuentes: List[dict], sembrar: bool = False) -> int:
    """Una ronda: mira cada fuente, detecta cambios y avisa. Devuelve nº de fallos.

    Se devuelve un contador en vez de tragarse los errores porque un vigilante
    que falla en silencio es peor que no tenerlo: si una tienda deja de
    responder, dejarías de recibir avisos sin enterarte.
    """
    conn = init_db(fuentes[0]["db_path"])
    errores = 0
    if sembrar:
        logging.info("Siembra: se guarda el estado actual SIN avisar.")

    for fuente in fuentes:
        if fuente["tipo"] == "shopify":
            actuales, completo, fallos = articulos_shopify(fuente)
        else:
            actuales, completo, fallos = articulos_html(fuente, conn)
        errores += fallos

        if not actuales:
            continue

        previos = {
            fila["clave"]: fila
            for fila in conn.execute(
                "SELECT * FROM articulos WHERE fuente_url = ?", (fuente["url"],)
            )
        }

        nuevos, repuestos, agotados = [], [], []
        momento = ahora()

        for art in actuales:
            anterior = previos.pop(art["clave"], None)

            if anterior is None:
                # Producto desconocido. El idioma de las tiendas en HTML está en
                # la ficha, así que se consulta solo ahora: no cambia nunca.
                if art["idioma"] is None and fuente["tipo"] == "html":
                    time.sleep(random.uniform(*DELAY_BETWEEN_URLS))
                    art["idioma"] = idioma_de_ficha(art["product_url"], fuente["selectors"])
                conn.execute(
                    "INSERT INTO articulos (clave, fuente_url, tienda, categoria, product_url,"
                    " titulo, precio, idioma, en_stock, primera_vez, ultimo_visto)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (art["clave"], art["fuente_url"], art["tienda"], art["categoria"],
                     art["product_url"], art["titulo"], art["precio"], art["idioma"],
                     art["en_stock"], momento, momento),
                )
                # Un producto que aparece ya agotado se guarda en silencio: no
                # es una novedad accionable, pero hay que conocerlo para poder
                # avisar cuando se reponga.
                if art["en_stock"] and not sembrar:
                    nuevos.append(art)
                    registrar_evento(conn, "nuevo", art)
                continue

            # El idioma ya se averiguó en su día: no se vuelve a pedir la ficha.
            art["idioma"] = art["idioma"] or anterior["idioma"]
            conn.execute(
                "UPDATE articulos SET titulo=?, precio=?, idioma=?, en_stock=?, ultimo_visto=?"
                " WHERE clave=?",
                (art["titulo"], art["precio"], art["idioma"], art["en_stock"],
                 momento, art["clave"]),
            )
            if not sembrar and art["en_stock"] and not anterior["en_stock"]:
                repuestos.append(art)
                registrar_evento(conn, "repuesto", art)
            elif not sembrar and not art["en_stock"] and anterior["en_stock"]:
                agotados.append(art)
                registrar_evento(conn, "agotado", art)

        # Lo que ya no aparece en un listado filtrado por stock está agotado.
        # Solo si el listado vino completo: si la paginación se cortó, la
        # ausencia no significa nada y marcarlo daría una avalancha de falsos.
        if completo:
            for clave, anterior in previos.items():
                if not anterior["en_stock"]:
                    continue
                art = {
                    "clave": clave, "tienda": anterior["tienda"],
                    "categoria": anterior["categoria"], "product_url": anterior["product_url"],
                    "titulo": anterior["titulo"], "precio": anterior["precio"],
                    "idioma": anterior["idioma"],
                }
                conn.execute(
                    "UPDATE articulos SET en_stock=0, ultimo_visto=? WHERE clave=?",
                    (momento, clave),
                )
                if not sembrar:
                    agotados.append(art)
                    registrar_evento(conn, "agotado", art)

        conn.commit()

        for tipo, lote in (("nuevo", nuevos), ("repuesto", repuestos), ("agotado", agotados)):
            for art in lote:
                if avisar_cambio(telegram, tipo, fuente["tienda"], art):
                    logging.info("Avisado [%s] %s: %s", tipo, fuente["tienda"], art["titulo"])
                else:
                    logging.error("No se pudo avisar de %s: %s", tipo, art["titulo"])
                    errores += 1


        time.sleep(random.uniform(*DELAY_BETWEEN_URLS))

    if sembrar:
        logging.info("Estado inicial guardado. A partir de la próxima ronda llegarán avisos.")
    else:
        total = conn.execute(
            "SELECT COUNT(*) c FROM articulos WHERE en_stock = 1"
        ).fetchone()["c"]
        logging.info("Ronda completada. %d artículos en stock ahora mismo.", total)

    if errores:
        logging.error("La ronda terminó con %d error(es).", errores)

    # Los mensajes de salud van al final, cuando ya se sabe cómo ha ido.
    if not sembrar:
        avisar_incidencias(telegram, conn, errores)
        latido(telegram, conn, fuentes[0]["horas_latido"])

    guardar_volcado(conn, fuentes[0]["db_path"])
    return errores


# --------------------------------------------------------------------------- #
# Informe diario
# --------------------------------------------------------------------------- #

def hora_local() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(ZONA_INFORME))
    except Exception:
        # Sin base de datos de zonas horarias, se cae a UTC y se avisa: es
        # preferible un informe a hora rara que ningún informe.
        logging.warning("No hay zona horaria '%s' disponible; se usa UTC.", ZONA_INFORME)
        return datetime.now(timezone.utc)


def informe_diario(telegram: dict, fuentes: List[dict], forzar: bool = False) -> int:
    """Resumen de los movimientos del día. Solo envía a las 20:00 locales.

    El cron de GitHub es en UTC y España cambia de hora dos veces al año, así
    que el workflow dispara a las dos horas candidatas y aquí se descarta la
    que no toca.
    """
    local = hora_local()
    if not forzar and local.hour != HORA_INFORME:
        logging.info("No son las %d:00 en %s (son las %s). No se envía informe.",
                     HORA_INFORME, ZONA_INFORME, local.strftime("%H:%M"))
        return 0

    conn = init_db(fuentes[0]["db_path"])
    desde = local.replace(hour=0, minute=0, second=0, microsecond=0)
    desde_utc = desde.astimezone(timezone.utc).isoformat(timespec="seconds")

    eventos = list(conn.execute(
        "SELECT * FROM eventos WHERE fecha >= ? ORDER BY tienda, tipo, id", (desde_utc,)
    ))

    fecha_texto = local.strftime("%d/%m/%Y")
    emojis = {f["categoria"]: f["emoji"] for f in fuentes}

    por_tienda = {}
    for ev in eventos:
        por_tienda.setdefault((ev["categoria"], ev["tienda"]), []).append(ev)

    # Se recorren las tiendas configuradas, no solo las que tuvieron
    # movimientos: si una no ha añadido nada, hay que decirlo. "Sin mensaje"
    # sería ambiguo — no distinguiría entre no hubo altas y el scraper falló.
    secciones = []
    vistas = []
    for fuente in fuentes:
        clave = (fuente["categoria"], fuente["tienda"])
        if clave in vistas:
            continue
        vistas.append(clave)

        emoji = emojis.get(fuente["categoria"], EMOJI_CATEGORIA_POR_DEFECTO)
        bloques = [f"{emoji} <b>{esc(fuente['categoria'])} — {esc(fuente['tienda'])}</b>"]

        lote = por_tienda.get(clave, [])
        altas = [e for e in lote if e["tipo"] == "nuevo"]
        otros = [e for e in lote if e["tipo"] != "nuevo"]

        if altas:
            bloques.extend(bloque_producto(ev) for ev in altas)
        else:
            bloques.append("No se ha añadido ningún producto")

        for ev in otros:
            _, etiqueta = ESTADOS.get(ev["tipo"], ("", ev["tipo"]))
            bloques.append(f"{etiqueta}\n{bloque_producto(ev)}")

        secciones.append("\n\n".join(bloques))

    en_stock = conn.execute("SELECT COUNT(*) c FROM articulos WHERE en_stock=1").fetchone()["c"]
    texto = (f"📋 <b>Resumen del {fecha_texto}</b>\n\n"
             + "\n\n\n".join(secciones)
             + f"\n\n\nTotal disponible ahora mismo: {en_stock} artículos.")

    logging.info("Informe diario: %d movimientos, %d artículos en stock.", len(eventos), en_stock)
    return 0 if enviar_mensaje(telegram, texto) else 1



# --------------------------------------------------------------------------- #
# Comandos de Telegram
# --------------------------------------------------------------------------- #

AYUDA = (
    "<b>Comandos disponibles</b>\n\n"
    "/stock — todo lo que hay en stock\n"
    "/stock flashstore — solo esa tienda\n"
    "/tiendas — qué se está vigilando\n"
    "/resumen — movimientos de hoy\n"
    "/ayuda — esto\n\n"
    "<i>Los comandos se atienden cada pocos minutos, no al instante.</i>"
)


def _comparable(texto: str) -> str:
    """'Flash Store' y 'Pokemillón' -> 'flashstore' y 'pokemillon'.

    Para que /stock flashstore encuentre "Flash Store" y /stock pokemillon
    encuentre "Pokemillón": al escribir un comando nadie pone el espacio ni
    la tilde.
    """
    return re.sub(r"[^a-z0-9]", "", slug(texto))


def texto_stock(conn, filtro: Optional[str], emojis: Optional[dict] = None) -> str:
    emojis = emojis or {}
    filas = list(conn.execute(
        "SELECT * FROM articulos WHERE en_stock = 1 ORDER BY tienda, categoria, titulo"
    ))
    if filtro:
        buscado = _comparable(filtro)
        filas = [f for f in filas if buscado in _comparable(f["tienda"])
                 or buscado in _comparable(f["categoria"])]

    if not filas:
        if filtro:
            tiendas = [f["tienda"] for f in conn.execute(
                "SELECT DISTINCT tienda FROM articulos")]
            return (f"No hay nada en stock que encaje con «{filtro}».\n"
                    f"Tiendas vigiladas: {', '.join(tiendas) or '—'}")
        return "Ahora mismo no hay ningún producto en stock."

    bloques = []
    grupo_actual = None
    for fila in filas:
        grupo = (fila["categoria"], fila["tienda"])
        if grupo != grupo_actual:
            grupo_actual = grupo
            emoji = emojis.get(fila["categoria"], EMOJI_CATEGORIA_POR_DEFECTO)
            bloques.append(f"{emoji} <b>{esc(fila['categoria'])} — {esc(fila['tienda'])}</b>")
        bloques.append(bloque_producto(fila))

    bloques.append(f"Total: {len(filas)} artículos en stock.")
    return "\n\n".join(bloques)



MENU_COMANDOS = [
    ("stock", "Todo lo que hay en stock ahora mismo"),
    ("tiendas", "Qué tiendas se están vigilando"),
    ("resumen", "Movimientos de hoy"),
    ("ayuda", "Lista de comandos"),
]


def registrar_menu(telegram: dict) -> int:
    """Publica el menú que Telegram sugiere al escribir "/" en el chat.

    Se hace por API (setMyCommands), no hace falta configurarlo a mano en
    BotFather. Basta con ejecutarlo una vez; Telegram lo recuerda.
    """
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{telegram['bot_token']}/setMyCommands",
            json={"commands": [{"command": c, "description": d} for c, d in MENU_COMANDOS]},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code != 200 or not r.json().get("ok"):
            logging.error("Telegram rechazó el menú de comandos: %s", r.text[:200])
            return 1
    except (requests.RequestException, ValueError) as e:
        logging.error("No se pudo registrar el menú: %s", e)
        return 1
    logging.info("Menú de comandos registrado: %s",
                 ", ".join("/" + c for c, _ in MENU_COMANDOS))
    return 0


def atender_comandos(telegram: dict, fuentes: List[dict]) -> int:
    """Lee los comandos nuevos de Telegram y responde.

    Se usa getUpdates con un offset guardado en la base, porque no hay ningún
    proceso encendido de forma continua: cada ejecución recoge lo que haya
    llegado desde la anterior.
    """
    conn = init_db(fuentes[0]["db_path"])
    offset = leer_estado(conn, "telegram_offset")
    parametros = {"timeout": 0}
    if offset:
        parametros["offset"] = int(offset)

    try:
        r = requests.get(
            f"https://api.telegram.org/bot{telegram['bot_token']}/getUpdates",
            params=parametros, timeout=REQUEST_TIMEOUT,
        )
        if r.status_code != 200:
            # El mensaje de error de requests incluiría la URL, y la URL lleva
            # el token dentro: se registra solo el código y la explicación.
            detalle = ""
            try:
                detalle = r.json().get("description", "")
            except ValueError:
                pass
            logging.error("Telegram devolvió HTTP %s al leer los comandos: %s",
                          r.status_code, detalle)
            if r.status_code == 401:
                logging.error("401 = token rechazado. Revisa el secret TELEGRAM_BOT_TOKEN.")
            return 1
        actualizaciones = r.json().get("result", [])
    except (requests.RequestException, ValueError) as e:
        logging.error("No se pudieron leer los comandos: %s", type(e).__name__)
        return 1

    if not actualizaciones:
        logging.info("Sin comandos nuevos.")
        return 0

    errores = 0
    ultimo = None
    for upd in actualizaciones:
        ultimo = upd["update_id"]
        try:
            errores += _atender_una(telegram, fuentes, conn, upd)
        except Exception:
            # Si una actualización concreta revienta, el offset debe avanzar
            # igualmente: si no, se reintentaría cada 5 minutos para siempre y
            # bloquearía todos los comandos siguientes.
            logging.exception("Fallo atendiendo la actualización %s", ultimo)
            errores += 1

    if ultimo is not None:
        escribir_estado(conn, "telegram_offset", ultimo + 1)
        guardar_volcado(conn, fuentes[0]["db_path"])
    return errores


def _atender_una(telegram: dict, fuentes: List[dict], conn, upd: dict) -> int:
    """Atiende una actualización. Devuelve 1 si no se pudo responder."""
    mensaje = upd.get("message") or upd.get("channel_post") or {}
    texto = (mensaje.get("text") or "").strip()
    chat = str((mensaje.get("chat") or {}).get("id", ""))

    if not texto.startswith("/") or not chat:
        return 0

    # El bot solo obedece al chat configurado: es público en Telegram y
    # cualquiera que lo encuentre podría pedirle el listado.
    if chat != str(telegram["chat_id"]):
        logging.warning("Comando ignorado de un chat desconocido: %s", chat)
        return 0

    partes = texto.split()
    orden = partes[0].split("@")[0].lower()
    argumento = " ".join(partes[1:]).strip() or None
    emojis = {f["categoria"]: f["emoji"] for f in fuentes}
    logging.info("Comando recibido: %s", texto)

    if orden in ("/stock", "/listado"):
        respuesta = texto_stock(conn, argumento, emojis)
    elif orden == "/tiendas":
        filas = conn.execute(
            "SELECT tienda, categoria, COUNT(*) t, SUM(en_stock) s"
            " FROM articulos GROUP BY tienda, categoria ORDER BY categoria, tienda"
        )
        bloques = []
        for f in filas:
            emoji = emojis.get(f["categoria"], EMOJI_CATEGORIA_POR_DEFECTO)
            bloques.append(
                f"{emoji} <b>{esc(f['categoria'])} — {esc(f['tienda'])}</b>\n"
                f"{f['s'] or 0} en stock de {f['t']} vigilados"
            )
        respuesta = "\n\n".join(bloques) or "Todavía no hay nada vigilado."
    elif orden == "/resumen":
        return informe_diario(telegram, fuentes, forzar=True)
    else:
        respuesta = AYUDA

    return 0 if enviar_mensaje(telegram, respuesta, chat_id=chat) else 1



# --------------------------------------------------------------------------- #
# Diagnóstico de tiendas nuevas
# --------------------------------------------------------------------------- #

PLATAFORMAS = [
    ("WooCommerce", ["woocommerce", "wp-content/plugins/woocommerce", "wc-block"]),
    ("Shopify", ["cdn.shopify.com", "shopify-features", "/collections/"]),
    ("PrestaShop", ["prestashop", "/modules/ps_"]),
    ("Magento", ["magento", "mage/cookies", "static/version"]),
]


def detectar_plataforma(html: str) -> str:
    bajo = html.lower()
    for nombre, pistas in PLATAFORMAS:
        if any(pista in bajo for pista in pistas):
            return nombre
    return "desconocida"


def sugerir_contenedores(soup) -> List[str]:
    """Adivina qué elemento repetido hace de ficha de producto.

    Se localizan los textos que parecen precio y se sube por sus ancestros
    anotando la firma de clases. Solo se proponen las que se repiten en la
    página y contienen un enlace, para descartar tanto el elemento del precio
    como el contenedor que envuelve a toda la parrilla.
    """
    candidatas = set()
    for nodo in soup.find_all(string=re.compile(r"\d+[.,]\d{2}\s*€")):
        ancestro = nodo.parent
        for _ in range(4):
            if ancestro is None or ancestro.name in ("body", "html"):
                break
            clases = [c for c in (ancestro.get("class") or []) if not c.isdigit()]
            if clases:
                candidatas.add("." + ".".join(clases[:3]))
            ancestro = ancestro.parent

    resultados = []
    for firma in candidatas:
        try:
            elementos = soup.select(firma)
        except Exception:
            continue
        if len(elementos) < 2:
            continue
        con_enlace = sum(1 for e in elementos if e.select_one("a[href]"))
        if con_enlace < len(elementos) * 0.8:
            continue
        resultados.append((firma, len(elementos)))

    resultados.sort(key=lambda par: (-par[1], par[0].count(".")))
    return [f"{firma}  ({veces} fichas)" for firma, veces in resultados[:5]]


def diagnosticar(url: str, selectores: dict) -> int:
    """Analiza una URL y dice si el scraper la entiende, sin tocar nada."""
    print(f"\n🔍 Analizando {url}\n")

    fuente = {
        "url": url, "categoria": "(prueba)", "tienda": "(prueba)",
        "selectors": selectores, "max_pages": 3,
        "tipo": "shopify" if es_shopify(url) else "html",
    }

    if fuente["tipo"] == "shopify":
        print("   Colección de Shopify: se leerá por su API JSON, porque muchos")
        print("   temas pintan la parrilla con JavaScript y el HTML llega vacío.\n")
        articulos, _, errores = articulos_shopify(fuente)
        if errores or not articulos:
            print("❌ No se pudo leer la colección por la API.\n")
            return 1
    else:
        try:
            resp = requests.get(
                url, headers={"User-Agent": USER_AGENT, "Accept-Language": "es-ES,es;q=0.9"},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as e:
            print(f"❌ No se pudo descargar: {e}\n")
            return 1
        print(f"   HTTP {resp.status_code}  →  {resp.url}")
        if resp.status_code != 200:
            print("❌ La tienda no devolvió una página válida.\n")
            return 1

        soup = BeautifulSoup(resp.text, "html.parser")
        print(f"   Plataforma detectada: {detectar_plataforma(resp.text)}")
        contador = soup.select_one(".woocommerce-result-count, .product-count, .toolbar-amount")
        if contador:
            print(f"   Contador de la tienda: {contador.get_text(' ', strip=True)}")

        articulos, _, errores = articulos_html(fuente, None)
        if not articulos:
            print("\n❌ El scraper NO entiende esta tienda con los selectores actuales.")
            sugerencias = sugerir_contenedores(soup)
            if sugerencias:
                print("\n   Posibles contenedores de producto:")
                for sug in sugerencias:
                    print(f"     {sug}")
                print('\n   Ponlo en la entrada:  "selectors": { "container": ".loquesea" }')
            else:
                print("   No hay ni precios en el HTML: la tienda carga el catálogo")
                print("   por JavaScript y habría que atacar su API.")
            print()
            return 1

    en_stock = [a for a in articulos if a["en_stock"]]
    print(f"\n   Artículos detectados: {len(articulos)}")
    print(f"   De ellos, en stock:   {len(en_stock)}")
    con_idioma = sum(1 for a in articulos if a["idioma"])
    print(f"   Con idioma detectado: {con_idioma}/{len(articulos)}"
          + ("  (en HTML se lee de la ficha al descubrirlos)" if fuente["tipo"] == "html" else ""))

    print("\n   Muestra:\n")
    for art in (en_stock or articulos)[:3]:
        print(f"     • {art['titulo']}")
        print(f"       precio: {art['precio'] or '(no detectado)'}")
        print(f"       idioma: {art['idioma'] or '(se leerá de la ficha)'}")
        print(f"       enlace: {art['product_url']}")

    print("\n   Añádela a fuentes.json así:")
    print('     { "url": "%s", "categoria": "TU_CATEGORIA" }' % url)
    print("\n✅ Esta URL se puede añadir.\n")
    return 0


# --------------------------------------------------------------------------- #
# Línea de comandos
# --------------------------------------------------------------------------- #

FUENTES_POR_DEFECTO = "fuentes.json"


def mostrar_fuentes(path: str, fuentes: List[dict]) -> int:
    """Dice dónde está el fichero de fuentes y cómo añadir entradas nuevas."""
    ruta = Path(path).resolve()
    categorias = sorted({f["categoria"] for f in fuentes})
    print(f"\n📄 Fichero de fuentes: {ruta}")
    print(f"   {len(fuentes)} URL(s) vigiladas en {len(categorias)} categoría(s)")
    print(f"   Base de datos: {Path(fuentes[0]['db_path']).resolve()}\n")

    for categoria in categorias:
        print(f"   {categoria}")
        for fuente in fuentes:
            if fuente["categoria"] == categoria:
                print(f"     {fuente['tienda']}  [{fuente['tipo']}]")
                print(f"       · {fuente['url']}")
        print()

    print("   Para añadir una URL, edita el fichero y mete una entrada en \"fuentes\":")
    print('     { "url": "https://latienda.es/cat/one-piece?stock_status=instock",')
    print('       "categoria": "One Piece" }')
    print("\n   Norma: la URL debe ir filtrada por stock (?stock_status=instock en")
    print("   WooCommerce). En Shopify no hace falta: la API da la disponibilidad.")
    print("\n   Antes de añadirla:   python3 scraper.py --check \"URL\"")
    print("   Después de añadirla: python3 scraper.py --seed\n")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="TCG Watcher - vigila tiendas de cartas y avisa por Telegram"
    )
    parser.add_argument("fuentes", nargs="?", default=FUENTES_POR_DEFECTO,
                        help=f"Fichero de fuentes (por defecto: {FUENTES_POR_DEFECTO})")
    parser.add_argument("--check", metavar="URL",
                        help="Analiza una URL sin tocar la base de datos ni enviar nada.")
    parser.add_argument("--seed", action="store_true",
                        help="Guarda el estado actual SIN avisar. Úsalo al añadir tiendas.")
    parser.add_argument("--listar", action="store_true",
                        help="Muestra qué se vigila y cómo añadir más.")
    parser.add_argument("--informe", action="store_true",
                        help=f"Envía el resumen diario (solo si son las {HORA_INFORME}:00 en {ZONA_INFORME}).")
    parser.add_argument("--forzar", action="store_true",
                        help="Con --informe, lo envía sea la hora que sea.")
    parser.add_argument("--comandos", action="store_true",
                        help="Atiende los comandos de Telegram pendientes.")
    parser.add_argument("--sin-avisos", action="store_true", dest="sin_avisos",
                        help="No envía nada a Telegram: solo registra lo que enviaría. "
                             "Úsalo para probar en local sin duplicar los avisos de la nube.")
    parser.add_argument("--menu", action="store_true",
                        help="Publica el menú de comandos que Telegram sugiere al escribir '/'.")
    args = parser.parse_args()

    if args.sin_avisos:
        globals()["SILENCIO"] = True

    if args.check:
        selectores = DEFAULT_SELECTORS
        if Path(args.fuentes).is_file():
            _, fuentes = load_fuentes(args.fuentes)
            for fuente in fuentes:
                if fuente["url"] == args.check:
                    selectores = fuente["selectors"]
        setup_logging()
        sys.exit(diagnosticar(args.check, selectores))

    if not Path(args.fuentes).is_file():
        parser.error(f"No encuentro '{args.fuentes}'. Créalo (ver README) o indica su ruta.")

    telegram, fuentes = load_fuentes(args.fuentes)

    if args.listar:
        sys.exit(mostrar_fuentes(args.fuentes, fuentes))

    setup_logging()

    if args.informe:
        sys.exit(1 if informe_diario(telegram, fuentes, forzar=args.forzar) else 0)
    if args.menu:
        sys.exit(registrar_menu(telegram))
    if args.comandos:
        sys.exit(1 if atender_comandos(telegram, fuentes) else 0)

    try:
        sys.exit(1 if run(telegram, fuentes, sembrar=args.seed) else 0)
    except SystemExit:
        raise
    except Exception as e:
        # Una excepción no controlada dejaría el vigilante mudo sin que nadie
        # se entere, así que se avisa por Telegram antes de caer.
        logging.exception("Fallo no controlado durante la ronda")
        enviar_mensaje(
            telegram,
            "🛑 <b>El vigilante ha fallado</b>\n\n"
            f"Error no controlado: {esc(type(e).__name__)}: {esc(str(e)[:300])}"
            "\n\nHay que revisar el código."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
