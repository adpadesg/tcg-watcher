#!/usr/bin/env python3
"""
TCG Watcher
-----------
Motor genérico de scraping para tiendas WooCommerce (Flash Store y similares).
Se ejecuta pasándole un fichero de configuración de categoría:

    python3 scraper.py config_one_piece.json

Cada config define: nombre de categoría, lista de URLs a vigilar,
credenciales de Telegram y ruta de la base de datos local (SQLite)
donde se guardan los productos ya vistos.

Diseñado para lanzarse cada X minutos vía launchd (ver README.md).
"""

import argparse
import json
import logging
import os
import random
import re
import sqlite3
import sys
import unicodedata
import time
from pathlib import Path
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = 20
DELAY_BETWEEN_URLS = (2, 5)  # segundos, con jitter, para no machacar la web


# Selectores por defecto: WooCommerce estándar (la mayoría de tiendas de cartas).
# Cada config puede sobreescribir los que necesite en su bloque "selectors",
# y cada URL puede además llevar los suyos propios si esa tienda usa otra
# plantilla. Las listas se prueban en orden: gana la primera que encuentre algo.
DEFAULT_SELECTORS = {
    "container": "li.product, .products .product, .product-grid-item",
    # Contenedores a descartar: WooCommerce marca las subcategorías del
    # catálogo con la clase "product", pero no son productos.
    "skip_classes": ["product-category", "product-cat"],
    "link": [
        "h2 a",
        "h3 a",
        ".woocommerce-loop-product__title a",
        "a.woocommerce-LoopProduct-link",
        "a.wd-product-img-link",
        "a[href*='/producto/']",
        "a[href*='/product/']",
        "a[href]",
    ],
    "title": [
        ".wd-entities-title",
        ".woocommerce-loop-product__title",
        ".product-title",
        "h2",
        "h3",
    ],
    "price": [".price", ".product-price", ".amount"],
    "pagination": ".woocommerce-pagination a, nav.pagination a, .page-numbers a",
}


# --------------------------------------------------------------------------- #
# Configuración y logging
# --------------------------------------------------------------------------- #

def load_dotenv(path: str = ".env"):
    """Carga variables KEY=VALUE de un fichero .env local, si existe.

    Sirve para no guardar el token del bot dentro de la config (que sí se sube
    a GitHub). En GitHub Actions las variables vienen de los secrets del repo,
    así que allí este fichero simplemente no existe.
    """
    env_file = Path(path)
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def expand_env(value):
    """Sustituye ${VARIABLE} por su valor en el entorno."""
    if not isinstance(value, str):
        return value

    def repl(match):
        name = match.group(1)
        resolved = os.environ.get(name)
        if resolved is None:
            raise ValueError(
                f"La variable de entorno '{name}' no está definida. "
                f"Defínela en el fichero .env (local) o como secret del repo (GitHub Actions)."
            )
        return resolved

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", repl, value)


def slug(texto: str) -> str:
    """Convierte "Pokémon" en "pokemon", para nombrar ficheros sin sorpresas."""
    sin_tildes = unicodedata.normalize("NFKD", texto)
    sin_tildes = "".join(c for c in sin_tildes if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "_", sin_tildes.lower()).strip("_")


def derivar_tienda(url: str, nombres: dict) -> str:
    """Deduce el nombre de la tienda a partir del dominio.

    De 'flashstore.es' no hay forma fiable de sacar "Flash Store" —partir la
    palabra en dos requeriría un diccionario y fallaría tanto como acertaría—,
    así que se devuelve 'Flashstore' y el mapa 'nombres_tienda' de la config
    permite corregirlo de una vez para todas las URLs de ese dominio.
    """
    dominio = urlparse(url).netloc.split(":")[0].lower()
    if dominio.startswith("www."):
        dominio = dominio[4:]

    if dominio in nombres:
        return nombres[dominio]

    # Dominios de dos niveles (.co.uk, .com.es...): si nos quedáramos con la
    # penúltima parte, "shop.mitienda.co.uk" daría la tienda "Co".
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


def load_fuentes(path: str) -> List[dict]:
    """Lee el fichero de fuentes y lo agrupa en una configuración por categoría.

    El fichero es una lista plana de URLs: cada una declara a qué categoría
    pertenece y, opcionalmente, a qué tienda. No se impone ninguna jerarquía
    tienda→categoría porque no existe: una tienda puede tener varias URLs de
    una misma categoría, y varias categorías distintas, y eso cambia con el
    tiempo. Aquí se agrupan por categoría, que es la unidad de aviso y de
    memoria de "ya visto".
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

    selectores_base = dict(DEFAULT_SELECTORS)
    selectores_base.update(raiz.get("selectors", {}))
    max_pages_base = raiz.get("max_pages", 3)
    seed_pages_base = raiz.get("seed_max_pages", 50)

    por_categoria = {}
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
                f"(fuentes #{urls_vistas[url]} y #{i}). Bórrala de una de las dos."
            )
        urls_vistas[url] = i

        selectores = dict(selectores_base)
        selectores.update(entrada.get("selectors", {}))

        cfg = por_categoria.setdefault(
            categoria,
            {
                "categoria": categoria,
                "urls": [],
                "telegram": telegram,
                "db_path": f"seen_{slug(categoria)}.db",
                "max_pages": max_pages_base,
                "seed_max_pages": seed_pages_base,
            },
        )
        tipo = entrada.get("tipo", "auto")
        if tipo not in ("auto", "html", "shopify"):
            raise ValueError(
                f'La fuente #{i} de {path} tiene un "tipo" desconocido: {tipo!r}. '
                'Usa "auto", "html" o "shopify".'
            )
        if tipo == "auto":
            tipo = "shopify" if es_shopify(url) else "html"

        cfg["urls"].append(
            {
                "url": url,
                "tipo": tipo,
                # Solo para Shopify: descarta los productos agotados. Es lo que
                # hace el filtro ?filter.v.availability=1 de la propia tienda,
                # que la API JSON no aplica por sí sola.
                "solo_disponibles": bool(entrada.get("solo_disponibles", False)),
                "selectors": selectores,
                "tienda": entrada.get("tienda") or derivar_tienda(url, nombres),
                "max_pages": entrada.get("max_pages", max_pages_base),
                "seed_max_pages": entrada.get("seed_max_pages", seed_pages_base),
            }
        )

    if not por_categoria:
        raise ValueError(f"La lista 'fuentes' de {path} está vacía.")

    return list(por_categoria.values())


def setup_logging(log_path: str):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


# --------------------------------------------------------------------------- #
# Base de datos de productos ya vistos
# --------------------------------------------------------------------------- #

def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_products (
            product_url TEXT PRIMARY KEY,
            title TEXT,
            price TEXT,
            source_url TEXT,
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    return conn


def is_first_run(conn: sqlite3.Connection) -> bool:
    cur = conn.execute("SELECT COUNT(*) FROM seen_products")
    return cur.fetchone()[0] == 0


def already_seen(conn: sqlite3.Connection, product_url: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM seen_products WHERE product_url = ?", (product_url,)
    )
    return cur.fetchone() is not None


def mark_seen(conn: sqlite3.Connection, product_url: str, title: str, price: str, source_url: str):
    conn.execute(
        "INSERT OR IGNORE INTO seen_products (product_url, title, price, source_url) "
        "VALUES (?, ?, ?, ?)",
        (product_url, title, price, source_url),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# Scraping (WooCommerce estándar)
# --------------------------------------------------------------------------- #

def fetch_html(url: str) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "es-ES,es;q=0.9",
    }
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def clean_price(raw: str) -> str:
    if not raw:
        return ""
    raw = raw.replace("\xa0", " ").strip()
    # Si hay precio tachado + oferta, quédate con el último importe (el de oferta)
    prices = re.findall(r"[\d.,]+\s*€", raw)
    return prices[-1].strip() if prices else raw


def extract_image(container, base_url: str) -> Optional[str]:
    """Devuelve la mejor URL de imagen del producto.

    Las tiendas con lazy-load (Woodmart y similares) dejan un placeholder en
    `src` y la imagen real en data-src / srcset / <source srcset>, así que se
    prueban todas las variantes por orden de fiabilidad.
    """
    candidates = []

    img = container.select_one("img")
    if img:
        for attr in ("src", "data-src", "data-lazy-src"):
            candidates.append(img.get(attr))
        for attr in ("srcset", "data-srcset", "data-lazy-srcset"):
            candidates.append(_first_from_srcset(img.get(attr)))

    for source in container.select("picture source"):
        for attr in ("srcset", "data-srcset", "data-lazy-srcset"):
            candidates.append(_first_from_srcset(source.get(attr)))

    slide = container.select_one("[data-image-srcset]")
    if slide:
        candidates.append(_first_from_srcset(slide.get("data-image-srcset")))

    for src in candidates:
        if src and not src.startswith("data:"):
            url = urljoin(base_url, src.strip())
            # Telegram traga mal los .webp; estas tiendas sirven el .jpg original
            # bajo la misma ruta sin el sufijo (ej: foto.jpg.webp -> foto.jpg)
            if re.search(r"\.(jpe?g|png)\.webp$", url):
                url = url[: -len(".webp")]
            return url

    return None


def _first_from_srcset(srcset: Optional[str]) -> Optional[str]:
    if not srcset:
        return None
    return srcset.split(",")[0].strip().split(" ")[0]


def _first_match(container, selectors: List[str]):
    """Devuelve el primer elemento que encuentre alguno de los selectores.

    Se prueban en orden de prioridad (no en orden del documento), para que un
    selector específico y fiable gane a uno genérico de último recurso.
    """
    for selector in selectors:
        try:
            found = container.select_one(selector)
        except Exception:
            logging.warning("Selector inválido, se ignora: %s", selector)
            continue
        if found:
            return found
    return None


def parse_products(html: str, source_url: str, selectors: Optional[dict] = None) -> List[dict]:
    selectors = selectors or DEFAULT_SELECTORS
    soup = BeautifulSoup(html, "html.parser")

    products = []
    seen_urls = set()
    skip_classes = set(selectors.get("skip_classes", []))

    for container in soup.select(selectors["container"]):
        if skip_classes.intersection(container.get("class") or []):
            continue

        title_link = _first_match(container, selectors["link"])
        if not title_link or not title_link.get("href"):
            continue

        product_url = urljoin(source_url, title_link["href"])
        if product_url in seen_urls:
            continue

        title = title_link.get_text(strip=True)
        if not title:
            title_el = _first_match(container, selectors["title"])
            if title_el:
                title = title_el.get_text(strip=True)
        if not title:
            title = title_link.get("aria-label") or "(sin título)"

        price_el = _first_match(container, selectors["price"])
        price = clean_price(price_el.get_text(" ", strip=True)) if price_el else ""

        seen_urls.add(product_url)
        products.append(
            {
                "product_url": product_url,
                "title": title,
                "price": price,
                "image": extract_image(container, source_url),
            }
        )

    return products


def find_page_urls(html: str, source_url: str, max_pages: int,
                   pagination_selector: str = DEFAULT_SELECTORS["pagination"]) -> List[str]:
    """Devuelve las URLs de las páginas 2..N de una categoría paginada.

    Se lee el paginador de WooCommerce para saber cuántas páginas hay y se
    construyen las URLs con el patrón /page/N/. El total (incluida la primera,
    ya descargada) se limita a `max_pages`.
    """
    if max_pages <= 1:
        return []

    soup = BeautifulSoup(html, "html.parser")
    last = 1
    for a in soup.select(pagination_selector):
        text = a.get_text(strip=True)
        if text.isdigit():
            last = max(last, int(text))
        m = re.search(r"/page/(\d+)/?", a.get("href") or "")
        if m:
            last = max(last, int(m.group(1)))

    last = min(last, max_pages)

    # La query se conserva: si la URL lleva un filtro de la tienda
    # (?stock_status=instock y similares), perderlo al paginar traería
    # productos que el filtro excluía, y se notificarían como novedades.
    partes = urlparse(source_url)
    ruta = re.sub(r"/page/\d+$", "", partes.path.rstrip("/"))
    cola = f"?{partes.query}" if partes.query else ""
    base = f"{partes.scheme}://{partes.netloc}{ruta}"
    return [f"{base}/page/{n}/{cola}" for n in range(2, last + 1)]


# --------------------------------------------------------------------------- #
# Shopify (API JSON)
# --------------------------------------------------------------------------- #

SHOPIFY_POR_PAGINA = 250   # máximo que admite products.json
SHOPIFY_MAX_PAGINAS = 20   # tope de seguridad: 5.000 productos por colección


def es_shopify(url: str) -> bool:
    return "/collections/" in urlparse(url).path


def _formato_euros(valor: float) -> str:
    """4900.0 -> '4.900,00 €'"""
    return f"{valor:,.2f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".") + " €"


def _precio_shopify(producto: dict, solo_disponibles: bool) -> str:
    """Precio a mostrar de un producto de Shopify.

    Un producto puede tener varias variantes a precios muy distintos (p.ej.
    la versión en inglés a 279,99 € agotada y la japonesa a 84,90 € en stock).
    Enseñar uno cualquiera engañaría, así que: si solo interesan los
    disponibles, se ignoran los precios de las variantes agotadas; y si aun así
    quedan precios distintos, se indica que es un "desde".
    """
    variantes = producto.get("variants", [])
    if solo_disponibles:
        disponibles = [v for v in variantes if v.get("available")]
        if disponibles:
            variantes = disponibles

    precios = set()
    for variante in variantes:
        try:
            precios.add(float(variante["price"]))
        except (KeyError, TypeError, ValueError):
            continue

    if not precios:
        return ""
    if len(precios) == 1:
        return _formato_euros(precios.pop())
    return "desde " + _formato_euros(min(precios))


def productos_shopify(fuente: dict, solo_disponibles: bool) -> (List[dict], int):
    """Lee una colección de Shopify por su API JSON en vez de raspar el HTML.

    Muchos temas de Shopify pintan la parrilla de productos con JavaScript, así
    que el HTML que llega no contiene el listado (o contiene solo widgets de
    recomendaciones, que darían avisos de productos equivocados). products.json
    es parte de Shopify, no del tema, así que no se rompe cuando la tienda
    cambia de diseño, y además trae la disponibilidad de cada variante.
    """
    partes = urlparse(fuente["url"])
    trozos = [t for t in partes.path.split("/") if t]
    try:
        handle = trozos[trozos.index("collections") + 1]
    except (ValueError, IndexError):
        logging.error("No se pudo sacar la colección de %s", fuente["url"])
        return [], 1

    origen = f"{partes.scheme}://{partes.netloc}"
    api = f"{origen}/collections/{handle}/products.json"

    productos = []
    descartados = 0
    errores = 0

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
            errores += 1
            break

        for prod in lote:
            disponible = any(v.get("available") for v in prod.get("variants", []))
            if solo_disponibles and not disponible:
                descartados += 1
                continue
            imagenes = prod.get("images") or []
            productos.append(
                {
                    "product_url": f"{origen}/products/{prod['handle']}",
                    "title": prod.get("title") or "(sin título)",
                    "price": _precio_shopify(prod, solo_disponibles),
                    "image": imagenes[0].get("src") if imagenes else None,
                }
            )

        if len(lote) < SHOPIFY_POR_PAGINA:
            break
        time.sleep(random.uniform(*DELAY_BETWEEN_URLS))

    detalle = f" ({descartados} agotados descartados)" if descartados else ""
    logging.info("%s -> %d productos%s", api, len(productos), detalle)

    if not productos and not errores:
        logging.error("0 productos en %s: ¿ha cambiado la colección?", api)
        errores += 1

    return productos, errores


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

MAX_PHOTO_BYTES = 10 * 1024 * 1024  # límite de Telegram para sendPhoto


def build_caption(product: dict, categoria: str) -> str:
    caption = f"🆕 [{categoria}] {product['title']}"
    if product["price"]:
        caption += f"\n💰 {product['price']}"
    caption += f"\n🔗 {product['product_url']}"
    return caption


def download_image(url: str) -> Optional[bytes]:
    """Descarga la imagen del producto para subirla a Telegram.

    Muchas tiendas bloquean a los servidores de Telegram si se les pasa la URL
    directamente (sendPhoto responde "failed to get HTTP URL content"), así que
    se descarga aquí, con cabeceras de navegador, y se sube el fichero.
    """
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Referer": url},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logging.warning("No se pudo descargar la imagen %s: %s", url, e)
        return None

    if not resp.headers.get("Content-Type", "").startswith("image/"):
        logging.warning("La URL de imagen no devolvió una imagen: %s", url)
        return None
    if len(resp.content) > MAX_PHOTO_BYTES:
        logging.warning("Imagen demasiado grande para Telegram (%d bytes): %s", len(resp.content), url)
        return None
    return resp.content


def send_telegram(bot_token: str, chat_id: str, product: dict, categoria: str) -> bool:
    """Envía el aviso. Devuelve True solo si Telegram lo ha aceptado."""
    caption = build_caption(product, categoria)
    api = f"https://api.telegram.org/bot{bot_token}"

    image_url = product.get("image")
    if image_url:
        data = download_image(image_url)
        if data:
            filename = image_url.split("/")[-1].split("?")[0] or "producto.jpg"
            try:
                r = requests.post(
                    f"{api}/sendPhoto",
                    data={"chat_id": chat_id, "caption": caption},
                    files={"photo": (filename, data)},
                    timeout=REQUEST_TIMEOUT,
                )
                if r.status_code == 200:
                    return True
                logging.warning("sendPhoto falló (%s), se envía como texto", r.text[:200])
            except requests.RequestException as e:
                logging.warning("Excepción en sendPhoto (%s), se envía como texto", e)

    try:
        r = requests.post(
            f"{api}/sendMessage",
            data={"chat_id": chat_id, "text": caption},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code != 200:
            logging.error("Error enviando a Telegram: %s", r.text[:300])
            return False
        return True
    except requests.RequestException as e:
        logging.error("Excepción enviando a Telegram: %s", e)
        return False


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
    """Busca a ojo qué elemento repetido podría ser la ficha de producto.

    Estrategia: localizar los textos que parecen un precio, subir por sus
    ancestros anotando la "firma" de clases, y quedarse solo con las firmas
    que de verdad parecen una ficha: deben repetirse en la página (una por
    producto) y contener un enlace. Así se descartan tanto el propio elemento
    del precio como el contenedor que envuelve a toda la parrilla.
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
            continue  # envoltorio de la parrilla, no una ficha
        con_enlace = sum(1 for e in elementos if e.select_one("a[href]"))
        if con_enlace < len(elementos) * 0.8:
            continue  # el precio suelto y similares: no llevan enlace
        resultados.append((firma, len(elementos)))

    resultados.sort(key=lambda par: (-par[1], par[0].count(".")))
    return [f"{firma}  ({veces} fichas)" for firma, veces in resultados[:5]]


def diagnosticar(url: str, selectors: dict) -> int:
    """Analiza una categoría y dice si el scraper la entiende tal cual."""
    print(f"\n🔍 Analizando {url}\n")

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "es-ES,es;q=0.9"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        print(f"❌ No se pudo descargar: {e}")
        return 1

    print(f"   HTTP {resp.status_code}   →  {resp.url}")
    if resp.status_code != 200:
        print("❌ La tienda no devolvió una página válida.")
        return 1

    html = resp.text
    soup = BeautifulSoup(html, "html.parser")
    plataforma = detectar_plataforma(html)
    print(f"   Plataforma detectada: {plataforma}")

    if es_shopify(str(resp.url)):
        print("\n   Es una colección de Shopify: se leerá por su API JSON en vez")
        print("   de raspar el HTML (muchos temas pintan la parrilla por JavaScript).")
        fuente = {"url": str(resp.url)}
        todos, errores = productos_shopify(fuente, solo_disponibles=False)
        disponibles, _ = productos_shopify(fuente, solo_disponibles=True)
        if errores or not todos:
            print("\n❌ No se pudo leer la colección por la API.")
            return 1
        print(f"\n   Productos en la colección: {len(todos)}")
        print(f"   De ellos, disponibles:     {len(disponibles)}")
        print("\n   Muestra:\n")
        for prod in disponibles[:3] or todos[:3]:
            print(f"     • {prod['title']}")
            print(f"       precio: {prod['price'] or '(no detectado)'}")
            print(f"       imagen: {'sí' if prod['image'] else 'NO detectada'}")
            print(f"       enlace: {prod['product_url']}")
        print("\n   Añádela así (quita solo_disponibles si quieres también los agotados):")
        print('     { "url": "%s",' % url)
        print('       "categoria": "TU_CATEGORIA", "solo_disponibles": true }')
        print("\n✅ Esta URL se puede añadir.\n")
        return 0

    contador = soup.select_one(".woocommerce-result-count, .product-count, .toolbar-amount")
    if contador:
        print(f"   Contador de la tienda: {contador.get_text(' ', strip=True)}")

    productos = parse_products(html, str(resp.url), selectors)
    paginas = find_page_urls(html, str(resp.url), 999, selectors["pagination"])
    print(f"   Contenedores encontrados: {len(soup.select(selectors['container']))}")
    print(f"   Productos reconocidos:    {len(productos)}")
    print(f"   Páginas detectadas:       {len(paginas) + 1}")

    if not productos:
        print("\n❌ El scraper NO entiende esta tienda con los selectores actuales.")
        sugerencias = sugerir_contenedores(soup)
        if sugerencias:
            print("\n   Posibles contenedores de producto (por frecuencia):")
            for sug in sugerencias:
                print(f"     {sug}")
            print('\n   Prueba a poner uno en el bloque "selectors" de la config:')
            print('     "selectors": { "container": ".loquesea" }')
        else:
            print("   No se han encontrado ni precios en la página: puede que la")
            print("   tienda cargue el catálogo por JavaScript, y entonces habría")
            print("   que atacar su API en vez del HTML.")
        return 1

    print("\n   Muestra de lo que se detectaría:\n")
    for prod in productos[:3]:
        print(f"     • {prod['title']}")
        print(f"       precio: {prod['price'] or '(no detectado)'}")
        print(f"       imagen: {'sí' if prod['image'] else 'NO detectada'}")
        print(f"       enlace: {prod['product_url']}")

    sin_precio = sum(1 for p in productos if not p["price"])
    sin_imagen = sum(1 for p in productos if not p["image"])
    print()
    if sin_precio:
        print(f"   ⚠️  {sin_precio}/{len(productos)} productos sin precio detectado.")
    if sin_imagen:
        print(f"   ⚠️  {sin_imagen}/{len(productos)} productos sin imagen (llegarán como texto).")
    if not sin_precio and not sin_imagen:
        print("   ✅ Título, precio e imagen detectados en todos los productos.")
    print(f"\n✅ Esta URL se puede añadir tal cual a una config.\n")
    return 0


# --------------------------------------------------------------------------- #
# Ejecución principal
# --------------------------------------------------------------------------- #

def productos_html(fuente: dict, max_pages: int) -> (List[dict], int):
    """Raspa una categoría en HTML, recorriendo su paginación."""
    source_url = fuente["url"]
    selectors = fuente["selectors"]
    productos = []
    vistos = set()
    errores = 0

    try:
        html = fetch_html(source_url)
    except requests.RequestException as e:
        logging.error("No se pudo descargar %s: %s", source_url, e)
        return [], 1

    paginas = [(source_url, html)]
    for page_url in find_page_urls(html, source_url, max_pages, selectors["pagination"]):
        time.sleep(random.uniform(*DELAY_BETWEEN_URLS))
        try:
            paginas.append((page_url, fetch_html(page_url)))
        except requests.RequestException as e:
            logging.error("No se pudo descargar %s: %s", page_url, e)
            errores += 1

    for page_url, page_html in paginas:
        encontrados = parse_products(page_html, page_url, selectors)
        logging.info("%s -> %d productos encontrados", page_url, len(encontrados))

        if not encontrados:
            # La página se descargó pero no se reconoció ningún producto:
            # señal de que la tienda ha cambiado de plantilla.
            logging.error(
                "0 productos en %s: puede que la tienda haya cambiado su HTML "
                "y haya que revisar los selectores de esta fuente.",
                page_url,
            )
            errores += 1

        for producto in encontrados:
            if producto["product_url"] not in vistos:
                vistos.add(producto["product_url"])
                productos.append(producto)

    return productos, errores


def run(cfg: dict, force_seed: bool = False) -> int:
    """Ejecuta una pasada. Devuelve el número de fallos (0 = todo bien).

    Se devuelve un contador en vez de tragarse los errores porque un vigilante
    que falla en silencio es peor que uno que no existe: si la tienda deja de
    responder o Telegram rechaza los envíos, dejarías de recibir avisos sin
    enterarte. Con esto, la ejecución de GitHub Actions se pone en rojo y te
    llega el aviso de fallo.
    """
    conn = init_db(cfg["db_path"])
    seed_mode = force_seed or is_first_run(conn)
    if seed_mode:
        logging.info(
            "Siembra de '%s': se guardan los productos actuales SIN notificar.",
            cfg["categoria"],
        )

    total_new = 0
    errores = 0

    for fuente in cfg["urls"]:
        source_url = fuente["url"]
        selectors = fuente["selectors"]
        tienda = fuente["tienda"]
        # El aviso identifica la tienda además de la categoría: la misma carta
        # puede aparecer en varias tiendas y son compras distintas.
        etiqueta = f"{cfg['categoria']} · {tienda}"
        max_pages = fuente["seed_max_pages"] if seed_mode else fuente["max_pages"]

        if fuente["tipo"] == "shopify":
            products, fallos = productos_shopify(fuente, fuente["solo_disponibles"])
        else:
            products, fallos = productos_html(fuente, max_pages)
        errores += fallos

        for product in products:
            if already_seen(conn, product["product_url"]):
                continue

            if not seed_mode:
                enviado = send_telegram(
                    cfg["telegram"]["bot_token"],
                    cfg["telegram"]["chat_id"],
                    product,
                    etiqueta,
                )
                if not enviado:
                    # No se marca como visto: así se reintenta en la siguiente
                    # pasada en vez de perder el aviso.
                    logging.error("Aviso NO enviado, se reintentará: %s", product["title"])
                    errores += 1
                    continue
                logging.info("Nuevo producto notificado: [%s] %s", tienda, product["title"])
                total_new += 1

            mark_seen(
                conn,
                product["product_url"],
                product["title"],
                product["price"],
                source_url,
            )

        time.sleep(random.uniform(*DELAY_BETWEEN_URLS))

    if seed_mode:
        logging.info("Catálogo guardado en la base de datos. A partir de la próxima ejecución llegarán notificaciones.")
    else:
        logging.info("Ejecución completada. Productos nuevos notificados: %d", total_new)

    if errores:
        logging.error("La pasada terminó con %d error(es).", errores)
    return errores


FUENTES_POR_DEFECTO = "fuentes.json"


def mostrar_fuentes(path: str, categorias: List[dict]) -> int:
    """Dice dónde está el fichero de fuentes y cómo añadir entradas nuevas."""
    ruta = Path(path).resolve()
    total = sum(len(c["urls"]) for c in categorias)
    print(f"\n📄 Fichero de fuentes: {ruta}")
    print(f"   {total} URL(s) vigiladas en {len(categorias)} categoría(s)\n")

    for cfg in sorted(categorias, key=lambda c: c["categoria"]):
        print(f"   {cfg['categoria']}   →  {cfg['db_path']}")
        por_tienda = {}
        for fuente in cfg["urls"]:
            por_tienda.setdefault(fuente["tienda"], []).append(fuente["url"])
        for tienda, urls in sorted(por_tienda.items()):
            print(f"     {tienda}")
            for url in urls:
                print(f"       · {url}")
        print()

    print("   Para añadir una URL, edita el fichero y mete una entrada en \"fuentes\":")
    print('     { "url": "https://latienda.es/cat/pokemon", "categoria": "Pokémon" }')
    print("\n   La tienda se deduce del dominio. Para forzar otro nombre:")
    print('     · para todas sus URLs:  "nombres_tienda": { "latienda.es": "La Tienda" }')
    print('     · solo para una URL:    añade "tienda": "La Tienda" a la entrada')
    print("\n   Antes de añadirla, compruébala:")
    print(f'     python3 scraper.py --check "https://latienda.es/cat/pokemon"')
    print("   Y después de añadirla, siembra para no recibir todo su catálogo de golpe:")
    print(f"     python3 scraper.py --seed\n")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="TCG Watcher - vigila categorías de tiendas y avisa por Telegram"
    )
    parser.add_argument(
        "fuentes",
        nargs="?",
        default=FUENTES_POR_DEFECTO,
        help=f"Fichero de fuentes (por defecto: {FUENTES_POR_DEFECTO})",
    )
    parser.add_argument(
        "--categoria",
        metavar="NOMBRE",
        help="Procesa solo esa categoría en vez de todas.",
    )
    parser.add_argument(
        "--check",
        metavar="URL",
        help="Analiza una URL y dice si el scraper la entiende, sin tocar la "
             "base de datos ni enviar nada. Úsalo antes de añadir una tienda nueva.",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Fuerza el modo siembra: guarda todo lo que encuentre SIN notificar. "
             "Úsalo al añadir tiendas o URLs nuevas.",
    )
    parser.add_argument(
        "--listar",
        action="store_true",
        help="Muestra dónde está el fichero de fuentes, qué vigila y cómo añadir más.",
    )
    args = parser.parse_args()

    if args.check:
        # --check no necesita fichero de fuentes; si existe, se usan sus
        # selectores, para poder validar los ajustes de una tienda concreta.
        selectores = DEFAULT_SELECTORS
        if Path(args.fuentes).is_file():
            categorias = load_fuentes(args.fuentes)
            for cfg in categorias:
                for fuente in cfg["urls"]:
                    if fuente["url"] == args.check:
                        selectores = fuente["selectors"]
        sys.exit(diagnosticar(args.check, selectores))

    if not Path(args.fuentes).is_file():
        parser.error(
            f"No encuentro el fichero de fuentes '{args.fuentes}'. "
            "Créalo (ver README) o indica su ruta como primer argumento."
        )

    categorias = load_fuentes(args.fuentes)

    if args.listar:
        sys.exit(mostrar_fuentes(args.fuentes, categorias))

    if args.categoria:
        elegidas = [c for c in categorias if slug(c["categoria"]) == slug(args.categoria)]
        if not elegidas:
            disponibles = ", ".join(sorted(c["categoria"] for c in categorias))
            parser.error(f"No hay ninguna categoría '{args.categoria}'. Hay: {disponibles}")
        categorias = elegidas

    setup_logging("tcg_watcher.log")

    errores = 0
    for cfg in categorias:
        errores += run(cfg, force_seed=args.seed)
    sys.exit(1 if errores else 0)


if __name__ == "__main__":
    main()
