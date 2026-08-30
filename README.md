# TCG Watcher

Vigila categorías de tiendas WooCommerce (Flash Store, etc.) y te avisa por
Telegram cuando aparece un producto nuevo.

Un único motor (`scraper.py`) + un JSON de configuración por categoría, para
poder vigilar varias categorías/tiendas sin duplicar código.

## 1. Instalación local (una vez)

```bash
cd ~/Desktop/tcg-watcher      # o donde hayas puesto la carpeta
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 2. Crear el bot de Telegram (una vez)

1. Abre Telegram, busca **@BotFather**.
2. Envíale `/newbot`, ponle nombre (ej. "Mi TCG Watcher").
3. Te da un **token** tipo `123456789:AAExxxxxxx...`. Cópialo.
4. Escríbele **cualquier mensaje** a tu bot nuevo para "activar" la conversación.
5. Para saber tu `chat_id`, visita en el navegador (con tu token):
   ```
   https://api.telegram.org/bot<TU_TOKEN>/getUpdates
   ```
   Busca `"chat":{"id":XXXXXXX` en la respuesta. Ese número es tu `chat_id`.

Si quieres un canal/chat distinto por categoría, crea un grupo/canal y añade el
bot como admin (el chat_id de un canal empieza por `-100...`).

## 3. Secretos: el fichero `.env`

**El token nunca va dentro de la config**, porque las configs sí se suben a
GitHub. Va en un `.env` local, que está en `.gitignore`:

```
TELEGRAM_BOT_TOKEN=123456789:AAExxxxxxx...
TELEGRAM_CHAT_ID=5398720700
```

En las configs se referencia con `${VARIABLE}`. En GitHub Actions esas mismas
variables llegan desde los *secrets* del repositorio (ver sección 6).

## 4. Configurar una categoría

Edita `config_one_piece.json` o duplícalo para una tienda/categoría nueva:

```json
{
  "categoria": "One Piece",
  "urls": [
    "https://flashstore.es/categoria/one-piece/"
  ],
  "telegram": {
    "bot_token": "${TELEGRAM_BOT_TOKEN}",
    "chat_id": "${TELEGRAM_CHAT_ID}"
  },
  "db_path": "seen_one_piece.db",
  "log_path": "log_one_piece.log",
  "max_pages": 3,
  "seed_max_pages": 50
}
```

- `urls`: puedes poner varias, de la misma tienda o de tiendas distintas,
  siempre que sean WooCommerce.
- `max_pages`: páginas que se revisan en cada pasada normal. Las tiendas ordenan
  por novedad, así que con las primeras sobra (3 páginas = 36 productos de
  margen entre ejecuciones).
- `seed_max_pages`: páginas que se recorren en la **siembra** inicial, para
  guardar el catálogo entero y no avisar de productos viejos.
- Conviene apuntar a la **categoría padre** (ej. `/categoria/pokemon/`) en vez
  de a varias subcategorías: cubre todo lo nuevo con una sola URL.

## 5. Probar a mano

```bash
source venv/bin/activate
python3 scraper.py config_one_piece.json
```

**La primera vez no llega ningún mensaje** — es la *siembra*: guarda el catálogo
actual como "ya visto" para no bombardearte. A partir de la segunda ejecución,
solo avisa de lo nuevo.

Para forzar una prueba real de notificación, borra una fila de la base de datos
y vuelve a ejecutar:

```bash
python3 - <<'PY'
import sqlite3
c = sqlite3.connect('seen_one_piece.db')
c.execute("DELETE FROM seen_products WHERE product_url = ?", ('URL_DEL_PRODUCTO',))
c.commit()
PY
python3 scraper.py config_one_piece.json
```

### Añadir tiendas o URLs a una categoría ya en marcha

Usa `--seed` **después** de añadir la URL nueva. Si no, te llegaría de golpe una
notificación por cada producto del catálogo recién añadido:

```bash
python3 scraper.py config_one_piece.json --seed
```

## 6. Dejarlo corriendo solo: GitHub Actions

Corre en la nube, así que no depende de que el Mac esté encendido.
El workflow está en `.github/workflows/tcg-watcher.yml`.

### Puesta en marcha

1. Crea el repositorio en GitHub (**público**: los repos privados consumen
   minutos de Actions y cada 10 min se pasarían del plan gratuito; el token no
   se sube, va como *secret*).
2. Sube el proyecto:
   ```bash
   git remote add origin https://github.com/<TU_USUARIO>/tcg-watcher.git
   git push -u origin main
   ```
3. En el repo → **Settings → Secrets and variables → Actions → New repository
   secret**, crea dos:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. En la pestaña **Actions**, activa los workflows y lanza *TCG Watcher* a mano
   una vez (`Run workflow`) para comprobar que va.

### Cómo recuerda lo que ya ha visto

Los ficheros `seen_*.db` se versionan en el repo: al final de cada ejecución,
si hay productos nuevos, el workflow hace commit de la base de datos
actualizada. Por eso hay que subir los `.db` ya sembrados.

### Detalles a tener en cuenta

- El cron `*/10 * * * *` es **orientativo**: GitHub retrasa las ejecuciones
  programadas en horas punta (a veces 15-30 min).
- GitHub **desactiva los cron** de repos sin actividad durante 60 días. Como el
  workflow hace commits cuando hay novedades, se mantiene vivo solo; si la
  tienda está muy parada, entra y dale a *Run workflow*.
- Si la tienda bloquease las IPs de los runners de GitHub, el log del workflow
  lo mostrará como error de descarga. En ese caso habría que volver a un
  equipo propio.

## 7. Alternativa: launchd en el Mac

Hay una plantilla en `com.tcgwatcher.onepiece.plist`.

**Ojo:** si el proyecto vive en `~/Desktop` (o Documentos/Descargas), launchd
**no puede leerlo** por la protección TCC de macOS, y falla con
`PermissionError: Operation not permitted`. Para usar launchd hay que mover el
proyecto fuera de esas carpetas (ej. `~/tcg-watcher`) o dar Acceso Total al
Disco al binario de Python en Ajustes del Sistema.

```bash
cp com.tcgwatcher.onepiece.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tcgwatcher.onepiece.plist
# para pararlo:
launchctl bootout gui/$(id -u)/com.tcgwatcher.onepiece
```

Además, el Mac tiene que estar encendido y despierto.

## Notas técnicas

- Contenedores: se buscan `li.product` y `.products .product`. Se **descartan**
  los que llevan la clase `product-category`: WooCommerce marca también las
  subcategorías con la clase `product` y, si no se filtran, se notifican como
  si fueran productos.
- Imágenes: se prueban `src`, `data-src`, `data-lazy-src`, los `srcset` y los
  `<source>` de `<picture>`, para sobrevivir al lazy-load. Los `.jpg.webp` se
  convierten a `.jpg`.
- Telegram: la foto se **descarga y se sube** como fichero en vez de pasarle la
  URL. Muchas tiendas bloquean a los servidores de Telegram, y `sendPhoto` con
  URL falla con `failed to get HTTP URL content`. Si aun así falla, hay
  fallback a mensaje de texto.
- Paginación: se lee el paginador de WooCommerce y se recorren las páginas
  `/page/N/` hasta el límite de `max_pages`.
- Se guarda un `seen_<categoria>.db` (SQLite) por categoría.
- Hay una espera aleatoria de 2-5 s entre peticiones para no saturar la web.
