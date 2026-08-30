# TCG Watcher

Vigila tiendas de cartas y avisa por Telegram cuando **aparece un producto
nuevo** o cuando **algo se queda sin stock**. Corre solo en GitHub Actions.

**Alcance actual:** categoría One Piece en Flash Store y Pokemillón.

La unidad que se vigila es **producto + idioma**: un mismo producto puede estar
disponible en japonés y agotado en inglés, y son dos cosas distintas a la hora
de comprar.

---

## Qué hace

| Cuándo | Qué |
|---|---|
| Cada 30 min | Revisa las tiendas. Si hay cambios, avisa. Si no, **no envía nada** |
| Cada día a las 20:00 (España) | Resumen de los movimientos del día, o "hoy no ha habido movimientos" |
| Cada 5 min | Atiende los comandos que le escribas por Telegram |

### Estructura estándar de producto

Un producto se muestra **siempre igual**, en tres líneas, se enseñe donde se
enseñe (alta, baja, resumen diario o listado):

```
🇯🇵 Nombre del producto
24,99 €
🔗 Pincha aquí
```

Bandera del idioma + nombre, precio, y enlace corto. Así el ojo reconoce el
patrón sin leer el mensaje entero.

### Avisos

**Un mensaje por producto, siempre.** Aunque una misma ronda detecte diez
cambios, salen diez mensajes: cada aviso tiene que poder leerse y actuarse por
separado desde la notificación del móvil.

```
Se ha añadido un nuevo producto a Flash Store

🇯🇵 One Piece Base Shop Vol.2
66,90 €
🔗 Pincha aquí
```

Los tres tipos de aviso:

| Tipo | Título |
|---|---|
| Alta | `Se ha añadido un nuevo producto a **Tienda**` |
| Baja | `Se ha agotado un producto de **Tienda**` |
| Reposición | `Vuelve a estar disponible un producto de **Tienda**` |

Entre mensajes se espera algo más de un segundo, porque Telegram limita los
envíos hacia un mismo chat. Si aun así responde `429`, se espera lo que él
indique y se reintenta: un aviso perdido es un aviso que no existe.

### Mensajes de estado del servicio

Un vigilante roto se parece demasiado a un vigilante sin novedades, así que el
bot también informa de sí mismo:

| Mensaje | Cuándo |
|---|---|
| 💚 Sigue en marcha | Cada `horas_latido` horas (12 por defecto), con el recuento en stock |
| ⚠️ Problema en el vigilante | Cuando una ronda acaba con errores, **como mucho una vez por hora** |
| ✅ Todo vuelve a funcionar | En la primera ronda limpia tras un fallo |
| 🛑 El vigilante ha fallado | Ante una excepción no controlada |

El freno de una hora en los avisos de error es deliberado: si una tienda deja
de responder, sin él llegarían 48 mensajes al día repitiendo lo mismo.

### Comandos de Telegram

| Comando | Qué devuelve |
|---|---|
| `/stock` | Todo lo disponible, con título, precio e idioma |
| `/stock flashstore` | Solo esa tienda (acepta `flash store`, `Flash`, sin tildes) |
| `/tiendas` | Qué se está vigilando y cuánto hay en stock |
| `/resumen` | Los movimientos de hoy, sin esperar a las 20:00 |
| `/ayuda` | La lista de comandos |

El listado usa la estructura estándar de tres líneas. Con 141 artículos, un
`/stock` completo son unos **7 mensajes**; `/stock flashstore` son 2. Para
consultar a diario, filtrar por tienda es más cómodo.

**El menú que Telegram sugiere al escribir `/`** se publica desde el código,
no desde BotFather:

```bash
python3 scraper.py --menu
```

Basta ejecutarlo una vez; Telegram lo recuerda. Hay que repetirlo solo si
cambian los comandos.

**No son instantáneos.** GitHub Actions no está encendido de forma continua:
se despierta cada 5 minutos (su mínimo) y, en horas punta, los cron se
retrasan. Cuenta con 5-20 minutos de espera. Para respuesta inmediata haría
falta un webhook en un servicio aparte.

El bot solo obedece al chat configurado: es público en Telegram y cualquiera
que lo encuentre podría pedirle el listado.

---

## El fichero `fuentes.json`

Todo lo que se vigila se declara aquí, y se edita a mano. La unidad es **la
URL**: cada una dice a qué categoría pertenece y, si hace falta, a qué tienda.
No se impone jerarquía tienda→categoría porque no existe y cambia con el tiempo.

```json
{
  "telegram": {
    "bot_token": "${TELEGRAM_BOT_TOKEN}",
    "chat_id": "${TELEGRAM_CHAT_ID}"
  },
  "nombres_tienda": {
    "flashstore.es": "Flash Store",
    "pokemillon.com": "Pokemillón"
  },
  "db_path": "estado.db",
  "max_pages": 20,
  "fuentes": [
    { "url": "https://flashstore.es/categoria/one-piece/?stock_status=instock",
      "categoria": "One Piece" },
    { "url": "https://www.pokemillon.com/collections/one-piece?filter.v.availability=1",
      "categoria": "One Piece" }
  ]
}
```

Para ver dónde está y cómo añadir entradas:

```bash
python3 scraper.py --listar
```

### Añadir una URL

```json
{ "url": "https://latienda.es/cat/one-piece?stock_status=instock",
  "categoria": "One Piece" }
```

> **Norma: la URL va filtrada por stock.** En WooCommerce, con
> `?stock_status=instock`. En Shopify no hace falta: la API da la
> disponibilidad de cada variante. Al paginar se conserva la query, así que el
> filtro sigue aplicándose en las páginas 2, 3...

**La tienda se deduce del dominio.** De `flashstore.es` no se puede sacar
"Flash Store" automáticamente (partir la palabra requeriría un diccionario),
así que sale `Flashstore` y se corrige con `nombres_tienda` (para todas las URLs
de ese dominio) o con `"tienda"` en la entrada (solo para esa).

### Campos opcionales

| Campo | Para qué |
|---|---|
| `tienda` | Fuerza el nombre de tienda solo en esa URL |
| `tipo` | `auto` (por defecto), `html` o `shopify` |
| `selectors` | Ajusta el HTML si la tienda usa otra plantilla |
| `max_pages` | Páginas a recorrer, solo para esa URL |

---

## Añadir una tienda: el ciclo completo

**1. Comprobar que se entiende** — no toca la base de datos ni envía nada:

```bash
python3 scraper.py --check "https://latienda.es/categoria/one-piece/?stock_status=instock"
```

Dice la plataforma, cuántos artículos ve, cuántos en stock, cuántos con idioma,
y una muestra. Si no la entiende, propone qué contenedor de producto usar.

**2. Añadirla a `fuentes.json`.**

**3. Sembrar** — guarda el estado actual sin avisar, para no recibir de golpe
un aviso por cada producto del catálogo:

```bash
python3 scraper.py --seed
```

**4. Subirlo:** `git add -A && git commit -m "..." && git push`

### Tiendas con otra plantilla

Los selectores por defecto son los de WooCommerce. Si una tienda usa otra cosa,
se ajusta en su entrada, sin tocar Python:

```json
{
  "url": "https://otratienda.es/coleccion/one-piece",
  "categoria": "One Piece",
  "selectors": {
    "container": ".ficha-articulo",
    "link": ["h4 a", "a[href]"],
    "price": [".importe"],
    "atributos": ".tabla-caracteristicas"
  }
}
```

Las listas (`link`, `title`, `price`) se prueban **en orden**: gana el primer
selector que encuentre algo.

### Tiendas Shopify

Las URLs con `/collections/` se leen por `products.json`, **no** raspando el
HTML. No es un atajo: muchos temas de Shopify pintan la parrilla con
JavaScript, así que el HTML llega sin el listado. En Pokemillón el contenedor
viene vacío y las únicas fichas del HTML son widgets de recomendaciones —
raspar eso daría avisos de productos ajenos a la colección.

La API además da **una entrada por variante** con su precio y su
disponibilidad, que es de donde sale el idioma.

---

## Comandos

| Comando | Qué hace |
|---|---|
| `python3 scraper.py` | Una ronda de vigilancia |
| `python3 scraper.py --listar` | Qué se vigila y dónde se configura |
| `python3 scraper.py --check URL` | Analiza una tienda sin tocar nada |
| `python3 scraper.py --seed` | Guarda el estado actual sin avisar |
| `python3 scraper.py --informe` | Resumen diario (solo si son las 20:00 en España) |
| `python3 scraper.py --informe --forzar` | El resumen, sea la hora que sea |
| `python3 scraper.py --comandos` | Atiende los comandos pendientes de Telegram |
| `python3 scraper.py --menu` | Publica el menú de comandos en Telegram |

---

## Instalación local

```bash
cd ~/Desktop/tcg-watcher
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Secretos: el fichero `.env`

**El token nunca va en `fuentes.json`**, que sí se sube a GitHub. Va en un
`.env` local, ignorado por git:

```
TELEGRAM_BOT_TOKEN=123456789:AAExxxxxxx...
TELEGRAM_CHAT_ID=5398720700
```

En GitHub Actions esas variables llegan desde **Settings → Secrets and
variables → Actions**.

---

## Cómo recuerda el estado

En el repositorio se versiona **`estado.sql`**, un volcado en texto de la base
de datos. El fichero binario `estado.db` está ignorado y se reconstruye a
partir del volcado en cada ejecución.

Se guarda el texto y no el binario porque git almacena solo las líneas que
cambian; del SQLite guardaría los 300 KB enteros en cada commit, y con varias
actualizaciones al día el repositorio crecería sin control.

Se conservan **90 días** de historial de eventos; lo anterior se descarta al
guardar.

### Qué se guarda de cada artículo

`clave`, tienda, categoría, URL, **título, precio, idioma y si está en stock**,
más cuándo se vio por primera y última vez. **No se guardan imágenes**: solo
texto, para que la base sea pequeña y las consultas rápidas.

---

## Automatización

Tres workflows en `.github/workflows/`, todos con el mismo grupo de
concurrencia (`tcg-watcher`) para que **nunca coincidan**: los tres escriben el
estado y dos a la vez darían un conflicto.

| Workflow | Cron | Qué hace |
|---|---|---|
| `vigilancia.yml` | `*/30 * * * *` | La ronda de vigilancia |
| `comandos.yml` | `*/5 * * * *` | Responde a los comandos |
| `informe.yml` | `0 18,19 * * *` | El resumen diario |

El resumen recorre **todas las tiendas configuradas**, no solo las que
tuvieron movimientos: si una no ha añadido nada, lo dice explícitamente
("No se ha añadido ningún producto"). El silencio sería ambiguo — no
distinguiría entre "no hubo altas" y "el scraper falló".

El informe se dispara **a dos horas** porque 20:00 en España son las 18:00 UTC
en verano y las 19:00 en invierno; el script comprueba la hora local y descarta
la que no toca.

Los cron de GitHub son orientativos: en horas punta se retrasan. Y GitHub los
desactiva en repos sin actividad durante 60 días — como el workflow commitea
cuando hay cambios, se mantiene vivo solo.

---

## Notas técnicas

- **Fallos ruidosos**: el scraper termina con código 1 —y el workflow se pone
  en rojo, con aviso por correo— si una URL no se descarga, si una página
  devuelve 0 productos (señal de que la tienda cambió su HTML) o si Telegram
  rechaza un envío. Un vigilante que falla en silencio es peor que no tenerlo.
- **Nunca se marca "agotado" a la ligera**: si la paginación se corta por el
  tope o falla una página, el listado se considera incompleto y esa ronda **no
  deduce agotados**. Si no, una página caída provocaría una avalancha de falsos
  "se ha agotado".
- **El idioma se lee una sola vez.** En las tiendas en HTML está en la ficha
  del producto (`Idioma: Japonés`), no en el listado, así que se consulta al
  descubrir el producto y se guarda: no cambia nunca y no hay que volver a
  pedirlo en cada ronda.
- Hay artículos **sin idioma**: productos de Shopify cuyas variantes no usan
  una opción "Idioma". Se dejan como desconocido en vez de adivinarlo.
- **Subcategorías**: se descartan los contenedores con la clase
  `product-category`. WooCommerce marca también las subcategorías con la clase
  `product` y, sin filtrarlas, se notificarían como productos.
- **Una actualización que falle no bloquea las siguientes**: si atender un
  comando revienta, el offset avanza igualmente. Si no, ese mismo comando se
  reintentaría cada 5 minutos para siempre y ningún otro llegaría a atenderse.
- **Los errores de Telegram se registran sin el token**: el mensaje de error de
  `requests` incluye la URL, y la URL lleva el token dentro. Se registra solo
  el código HTTP y la descripción.
- Cada workflow **valida el token antes de trabajar**, para que un secret mal
  puesto dé un error claro en lugar de un fallo sin explicación.
- Espera aleatoria de 2-5 s entre peticiones para no saturar las tiendas.
