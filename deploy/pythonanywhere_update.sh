#!/bin/bash
# Safe update for a site already running on PythonAnywhere.
#
#   bash ~/Lumen-Market/deploy/pythonanywhere_update.sh
#
# Pulls the new code, installs packages, updates the database, collects
# static files, checks everything and only then reloads the website - so
# visitors never hit half-updated code (that is what causes short-lived
# "ImportError" page crashes in System check).
set -e

USER_NAME="$(whoami)"
APP_DIR="${APP_DIR:-$HOME/Lumen-Market}"
WSGI_FILE="/var/www/${USER_NAME}_pythonanywhere_com_wsgi.py"

step() { echo; echo "==> $1"; }

cd "$APP_DIR"
source venv/bin/activate

step "1/6 Backing up the database"
python manage.py backup_data || echo "   (backup failed - continuing)"

step "2/6 Getting the latest code"
git pull --ff-only

step "3/6 Installing packages"
pip install --quiet -r requirements.txt

step "4/6 Updating the database"
python manage.py migrate --noinput

step "5/6 Static files and checks"
python manage.py collectstatic --noinput >/dev/null
python manage.py check --deploy --fail-level ERROR

step "6/6 Reloading the website"
if [ -f "$WSGI_FILE" ]; then
  touch "$WSGI_FILE"
  echo "Reloaded. Open Admin > System check and press 'Mark errors as resolved' if old errors are listed."
else
  echo "Press the green Reload button on the PythonAnywhere Web tab."
fi

echo
echo "Make sure PythonAnywhere > Tasks has this daily task:"
echo "  cd $APP_DIR && venv/bin/python manage.py daily_tasks"
