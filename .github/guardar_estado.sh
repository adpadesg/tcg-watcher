#!/usr/bin/env bash
# Guarda estado.sql (el volcado de texto de la base) en el repo si ha cambiado. Cada ejecución en la nube
# arranca de cero, así que la base de datos versionada es la única memoria
# de lo que ya se ha avisado.
set -euo pipefail

git config user.name "tcg-watcher"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add estado.sql

if git diff --cached --quiet; then
  echo "Sin cambios que guardar."
  exit 0
fi

git commit -m "${1:-Actualiza estado} ($(date -u +'%Y-%m-%d %H:%M UTC'))"
git pull --rebase --autostash
git push
