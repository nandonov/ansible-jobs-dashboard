# Pin to a patched Python 3.11 slim on Debian Bookworm to avoid known critical CVEs
FROM python:3.11.9-slim-bookworm
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
	&& useradd -m appuser \
	&& mkdir -p /app/data \
	&& chown -R appuser:appuser /app
COPY ./app /app
USER appuser
EXPOSE 8000
# Basic health check to ensure the API is responsive
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
	CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/jobs').read()" || exit 1
# Run with a single worker to keep SQLite happy
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]