FROM python:3.9-slim

# Systemabhängigkeiten installieren
RUN apt-get update && apt-get install -y ffmpeg curl && rm -rf /var/lib/apt/lists/*

# Arbeitsverzeichnis festlegen
WORKDIR /usr/src/app

# Python-Abhängigkeiten installieren
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Projektdateien kopieren
COPY . .

# Flask- und Celery-Konfiguration setzen
ENV FLASK_APP=app.py
ENV FLASK_ENV=production

# Nicht-Root-Benutzer erstellen und verwenden
RUN useradd -m myuser
RUN chown -R myuser:myuser /usr/src/app
USER myuser

# Gunicorn als WSGI-Server verwenden
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]