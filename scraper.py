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
import time
from pathlib import Path
from typing import List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = 20
DELAY_BETWEEN_URLS = (2, 5)  # segundos, con jitter, para no machacar la web


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


def load_config(path: str) -> dict:
    load_dotenv()

    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    required = ["categoria", "urls", "telegram"]
    for key in required:
        if key not in cfg:
            raise ValueError(f"Falta la clave obligatoria '{key}' en {path}")
    if "bot_token" not in cfg["telegram"] or "chat_id" not in cfg["telegram"]:
        raise ValueError("La sección 'telegram' necesita 'bot_token' y 'chat_id'")
    cfg["telegram"] = {k: expand_env(v) for k, v in cfg["telegram"].items()}

    cfg.setdefault("db_path", f"seen_{cfg['categoria']}.db")
    cfg.setdefault("log_path", f"log_{cfg['categoria']}.log")
    # Páginas de cada categoría a revisar en cada pasada. Las tiendas ordenan
    # por novedad, así que con las primeras basta para el día a día; en la
    # siembra inicial se recorre el catálogo entero para tener base completa.
    cfg.setdefault("max_pages", 3)
    cfg.setdefault("seed_max_pages", 50)
    return cfg


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


def parse_products(html: str, source_url: str) -> List[dict]:
    soup = BeautifulSoup(html, "html.parser")

    containers = soup.select("li.product") + soup.select(".products .product")
    products = []
    seen_urls = set()

    for container in containers:
        classes = container.get("class") or []
        # Las subcategorías del catálogo también llevan la clase "product"
        # (WooCommerce las marca como product-category). No son productos.
        if "product-category" in classes:
            continue

        title_link = (
            container.select_one("h2 a")
            or container.select_one("h3 a")
            or container.select_one(".woocommerce-loop-product__title a")
            or container.select_one("a.woocommerce-LoopProduct-link")
            or container.select_one("a.wd-product-img-link")
            or container.select_one('a[href*="/producto/"]')
        )
        if not title_link:
            # último recurso: primer <a> con href que parezca ficha de producto
            title_link = container.select_one("a[href]")
        if not title_link or not title_link.get("href"):
            continue

        product_url = urljoin(source_url, title_link["href"])
        if product_url in seen_urls:
            continue

        title = title_link.get_text(strip=True)
        if not title:
            title_el = container.select_one(
                ".wd-entities-title, .woocommerce-loop-product__title, h2, h3"
            )
            if title_el:
                title = title_el.get_text(strip=True)
        if not title:
            title = title_link.get("aria-label") or "(sin título)"

        price_el = container.select_one(".price")
        price = clean_price(price_el.get_text(" ", strip=True)) if price_el else ""

        image = extract_image(container, source_url)

        seen_urls.add(product_url)
        products.append(
            {
                "product_url": product_url,
                "title": title,
                "price": price,
                "image": image,
            }
        )

    return products


def find_page_urls(html: str, source_url: str, max_pages: int) -> List[str]:
    """Devuelve las URLs de las páginas 2..N de una categoría paginada.

    Se lee el paginador de WooCommerce para saber cuántas páginas hay y se
    construyen las URLs con el patrón /page/N/. El total (incluida la primera,
    ya descargada) se limita a `max_pages`.
    """
    if max_pages <= 1:
        return []

    soup = BeautifulSoup(html, "html.parser")
    last = 1
    for a in soup.select(".woocommerce-pagination a, nav.pagination a, .page-numbers a"):
        text = a.get_text(strip=True)
        if text.isdigit():
            last = max(last, int(text))
        m = re.search(r"/page/(\d+)/?", a.get("href") or "")
        if m:
            last = max(last, int(m.group(1)))

    last = min(last, max_pages)
    base = source_url.split("?")[0].rstrip("/")
    base = re.sub(r"/page/\d+$", "", base)
    return [f"{base}/page/{n}/" for n in range(2, last + 1)]


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


def send_telegram(bot_token: str, chat_id: str, product: dict, categoria: str):
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
                    return
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
    except requests.RequestException as e:
        logging.error("Excepción enviando a Telegram: %s", e)


# --------------------------------------------------------------------------- #
# Ejecución principal
# --------------------------------------------------------------------------- #

def run(cfg: dict, force_seed: bool = False):
    conn = init_db(cfg["db_path"])
    seed_mode = force_seed or is_first_run(conn)
    if seed_mode:
        logging.info(
            "Primera ejecución para '%s': se guardan los productos actuales SIN notificar.",
            cfg["categoria"],
        )

    max_pages = cfg["seed_max_pages"] if seed_mode else cfg["max_pages"]
    total_new = 0

    for source_url in cfg["urls"]:
        try:
            html = fetch_html(source_url)
        except requests.RequestException as e:
            logging.error("No se pudo descargar %s: %s", source_url, e)
            continue

        pages = [(source_url, html)]
        for page_url in find_page_urls(html, source_url, max_pages):
            time.sleep(random.uniform(*DELAY_BETWEEN_URLS))
            try:
                pages.append((page_url, fetch_html(page_url)))
            except requests.RequestException as e:
                logging.error("No se pudo descargar %s: %s", page_url, e)

        for page_url, page_html in pages:
            products = parse_products(page_html, page_url)
            logging.info("%s -> %d productos encontrados", page_url, len(products))

            for product in products:
                if already_seen(conn, product["product_url"]):
                    continue

                mark_seen(
                    conn,
                    product["product_url"],
                    product["title"],
                    product["price"],
                    page_url,
                )

                if not seed_mode:
                    send_telegram(
                        cfg["telegram"]["bot_token"],
                        cfg["telegram"]["chat_id"],
                        product,
                        cfg["categoria"],
                    )
                    logging.info("Nuevo producto notificado: %s", product["title"])
                    total_new += 1

        time.sleep(random.uniform(*DELAY_BETWEEN_URLS))

    if seed_mode:
        logging.info("Catálogo guardado en la base de datos. A partir de la próxima ejecución llegarán notificaciones.")
    else:
        logging.info("Ejecución completada. Productos nuevos notificados: %d", total_new)


def main():
    parser = argparse.ArgumentParser(description="TCG Watcher - scraping de categorías WooCommerce")
    parser.add_argument("config", help="Ruta al fichero de configuración JSON de la categoría")
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Fuerza el modo siembra: guarda todo lo que encuentre SIN notificar. "
             "Úsalo al añadir tiendas o URLs nuevas a una categoría ya en marcha.",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg["log_path"])
    run(cfg, force_seed=args.seed)


if __name__ == "__main__":
    main()
