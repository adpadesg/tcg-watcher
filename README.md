# TCG Watcher

Vigila categorías de tiendas de cartas y te avisa por Telegram cuando aparece un
producto nuevo. Corre solo en GitHub Actions cada 10 minutos.

Todo lo que vigila se declara en un único fichero, **`fuentes.json`**, editable a
mano. Añadir una tienda o una categoría no requiere tocar código.

---

## El fichero `fuentes.json`

La unidad básica es **la URL**. Cada URL declara a qué **categoría** pertenece y,
opcionalmente, a qué **tienda**. No se impone ninguna jerarquía tienda→categoría:
una tienda puede tener varias URLs de la misma categoría y URLs de categorías
distintas, y eso cambia con el tiempo.

```json
{
  "telegram": {
    "bot_token": "${TELEGRAM_BOT_TOKEN}",
    "chat_id": "${TELEGRAM_CHAT_ID}"
  },

  "nombres_tienda": {
    "flashstore.es": "Flash Store"
  },

  "max_pages": 3,
  "seed_max_pages": 70,

  "fuentes": [
    { "url": "https://flashstore.es/categoria/one-piece/", "categoria": "One Piece" },
    { "url": "https://flashstore.es/categoria/pokemon/",   "categoria": "Pokémon" }
  ]
}
```

Para ver en cualquier momento dónde está el fichero, qué vigila y cómo añadir
entradas:

```bash
python3 scraper.py --listar
```

### Añadir una URL

Una entrada con dos datos. Todo lo demás es opcional:

```json
{ "url": "https://latienda.es/cat/pokemon", "categoria": "Pokémon" }
```

- **La categoría es libre**: el nombre que escribas es el que agrupa. Todas las
  URLs con la misma categoría comparten un hilo de avisos y una base de datos
  (`seen_<categoria>.db`), vengan de la tienda que vengan. El aviso indica de
  cuál viene: `🆕 [One Piece · Flash Store] ...`
- **La tienda se deduce del dominio** (`flashstore.es` → `Flashstore`).

### Forzar el nombre de una tienda

De un dominio no siempre se puede sacar un nombre bonito: partir `flashstore` en
"Flash Store" requeriría un diccionario. Hay dos formas de corregirlo:

| Cuándo | Cómo |
|---|---|
| Para **todas** las URLs de ese dominio | `"nombres_tienda": { "flashstore.es": "Flash Store" }` |
| Solo para **una** URL | añade `"tienda": "Flash Store"` a esa entrada |

### Campos opcionales de una entrada

| Campo | Para qué |
|---|---|
| `tienda` | Fuerza el nombre de tienda solo en esa URL |
| `tipo` | `auto` (por defecto), `html` o `shopify` |
| `solo_disponibles` | Solo Shopify: descarta los productos agotados |
| `selectors` | Ajusta el HTML si esa tienda usa otra plantilla (ver abajo) |
| `max_pages` | Páginas a revisar en cada pasada, solo para esa URL |
| `seed_max_pages` | Páginas a recorrer al sembrar, solo para esa URL |

### Tiendas Shopify

Las URLs con `/collections/` se detectan como Shopify y **no se raspan en
HTML**: se leen por `products.json`, la API que Shopify expone en todas sus
tiendas.

No es un atajo, es una necesidad: muchos temas de Shopify pintan la parrilla de
productos con JavaScript, así que el HTML que llega **no contiene el listado**.
En Pokemillón, por ejemplo, el contenedor de la parrilla viene vacío y las
únicas fichas del HTML son widgets de recomendaciones — raspar eso daría avisos
de productos que no pertenecen a la colección.

Ventajas de la API: trae la colección entera (sin depender de la paginación por
scroll infinito), no se rompe cuando la tienda cambia de tema, e incluye la
disponibilidad de cada variante.

```json
{
  "url": "https://www.pokemillon.com/collections/one-piece?filter.v.availability=1",
  "categoria": "One Piece",
  "solo_disponibles": true
}
```

- `solo_disponibles: true` reproduce el filtro `?filter.v.availability=1` de la
  tienda. La API no lo aplica sola, así que hay que pedirlo aquí.
- **El precio tiene en cuenta las variantes.** Un producto puede tener la
  versión inglesa a 279,99 € agotada y la japonesa a 84,90 € en stock; con
  `solo_disponibles` se ignoran los precios de las variantes agotadas, y si aun
  así quedan varios precios se muestra `desde 84,90 €`.
- **Efecto secundario a tener en cuenta:** con `solo_disponibles`, un producto
  agotado no se guarda. Si vuelve a stock, aparecerá como nuevo y te avisará
  — que suele ser justo lo que quieres. Pero solo la primera vez: a partir de
  ahí ya está en la base de datos.

En la raíz del fichero, `max_pages`, `seed_max_pages` y `selectors` fijan el
valor por defecto de todas las URLs.

---

## Añadir una tienda nueva: el ciclo completo

### 1. Comprobar que el scraper la entiende

```bash
python3 scraper.py --check "https://latienda.es/categoria/pokemon/"
```

No toca la base de datos ni envía nada. Informa de la plataforma detectada,
productos reconocidos, páginas y una muestra de título/precio/imagen.

- **✅ "Esta URL se puede añadir tal cual"** → sigue al paso 2.
- **❌ "El scraper NO entiende esta tienda"** → te propone qué contenedores de
  producto ha encontrado en el HTML, para ponerlos en `selectors`.

### 2. Añadirla a `fuentes.json`

### 3. Sembrar

Guarda el catálogo actual **sin notificar**, para que no te lleguen de golpe
cientos de avisos de productos que ya existían:

```bash
python3 scraper.py --seed
```

### 4. Subirlo

```bash
git add -A && git commit -m "Añade <tienda> a <categoría>" && git push
```

A partir de ahí entra en la ronda automática de los 10 minutos.

### Tiendas con otra plantilla

Los selectores por defecto son los de WooCommerce. Si una tienda usa otra cosa,
se ajusta en su propia entrada, **sin tocar Python**:

```json
{
  "url": "https://otratienda.es/coleccion/one-piece",
  "categoria": "One Piece",
  "selectors": {
    "container": ".ficha-articulo",
    "link": ["h4 a", "a[href]"],
    "price": [".importe"]
  }
}
```

- Las listas (`link`, `title`, `price`) se prueban **en orden**: gana el primer
  selector que encuentre algo, así uno fiable manda sobre el genérico.
- `skip_classes` descarta contenedores; por defecto `product-category`, que es
  como WooCommerce marca las subcategorías (si no se filtran, se notifican como
  si fueran productos).
- `pagination` es el selector de los enlaces de página.

---

## Comandos

| Comando | Qué hace |
|---|---|
| `python3 scraper.py` | Una pasada por todas las categorías |
| `python3 scraper.py --listar` | Dónde está `fuentes.json`, qué vigila y cómo añadir más |
| `python3 scraper.py --check URL` | Analiza una tienda sin tocar nada |
| `python3 scraper.py --seed` | Guarda el catálogo actual sin notificar |
| `python3 scraper.py --categoria "One Piece"` | Procesa solo esa categoría |

`--seed` y `--categoria` se combinan, para sembrar solo lo que acabas de añadir.

---

## Instalación local (una vez)

```bash
cd ~/Desktop/tcg-watcher
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### El bot de Telegram

1. En Telegram, busca **@BotFather** y envíale `/newbot`.
2. Te da un **token**. Escríbele cualquier mensaje al bot nuevo para activar la
   conversación.
3. Tu `chat_id` sale en `https://api.telegram.org/bot<TU_TOKEN>/getUpdates`,
   en `"chat":{"id":XXXXXXX`.

### Secretos: el fichero `.env`

**El token nunca va dentro de `fuentes.json`**, porque ese fichero sí se sube a
GitHub. Va en un `.env` local, que está en `.gitignore`:

```
TELEGRAM_BOT_TOKEN=123456789:AAExxxxxxx...
TELEGRAM_CHAT_ID=5398720700
```

En `fuentes.json` se referencian como `${TELEGRAM_BOT_TOKEN}`. En GitHub Actions
esas variables llegan desde los *secrets* del repositorio.

---

## Automatización (GitHub Actions)

El workflow está en `.github/workflows/tcg-watcher.yml` y corre cada 10 minutos.

- Los secrets `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` se configuran en
  **Settings → Secrets and variables → Actions**.
- Los ficheros `seen_*.db` **se versionan en el repo**: al final de cada
  ejecución, si hay productos nuevos, el workflow commitea la base de datos
  actualizada. Por eso hay que subir los `.db` ya sembrados.
- El cron es orientativo: GitHub retrasa las ejecuciones programadas en horas
  punta (a veces 15-30 min).
- GitHub desactiva los cron de repos sin actividad durante 60 días. Como el
  workflow commitea cuando hay novedades, se mantiene vivo solo.

---

## Notas técnicas

- **Contenedores**: se buscan `li.product`, `.products .product` y
  `.product-grid-item`, descartando los que llevan `product-category`.
- **Imágenes**: se prueban `src`, `data-src`, `data-lazy-src`, los `srcset` y
  los `<source>` de `<picture>`, para sobrevivir al lazy-load. Los `.jpg.webp`
  se convierten a `.jpg`.
- **Telegram**: la foto se **descarga y se sube** como fichero en vez de pasar
  la URL. Muchas tiendas bloquean a los servidores de Telegram y `sendPhoto`
  con URL falla con `failed to get HTTP URL content`. Si aun así falla, hay
  fallback a mensaje de texto.
- **Paginación**: se lee el paginador y se recorren las páginas `/page/N/`
  hasta `max_pages`, **conservando la query** de la URL. Si la URL lleva un
  filtro de la tienda (`?stock_status=instock`), perderlo al paginar traería
  productos que el filtro excluía y se notificarían como novedades.
- **Fallos ruidosos**: el scraper termina con código 1 —y el workflow se pone
  en rojo, con aviso por correo— si una URL no se descarga, si una página
  devuelve 0 productos (señal de que la tienda cambió su HTML) o si Telegram
  rechaza un envío. Un vigilante que falla en silencio es peor que no tenerlo.
- **Un aviso no se pierde**: el producto se marca como visto *después* de que
  Telegram confirme el envío, así que si falla se reintenta en la pasada
  siguiente.
- Espera aleatoria de 2-5 s entre peticiones para no saturar las tiendas.

## Alternativa: launchd en el Mac

Hay una plantilla en `com.tcgwatcher.onepiece.plist`. **Ojo:** si el proyecto
vive en `~/Desktop` (o Documentos/Descargas), launchd no puede leerlo por la
protección TCC de macOS y falla con `PermissionError: Operation not permitted`.
Habría que mover el proyecto a `~/tcg-watcher` o dar Acceso Total al Disco al
binario de Python.
