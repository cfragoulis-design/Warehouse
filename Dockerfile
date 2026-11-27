# Production image for sklavounoswh: same Python as runtime.txt, plus Piper + ffmpeg
# so Telegram «πες μου» / «Αθηνά» questions can be spoken without an AI service.
FROM python:3.11.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIPER_HOME=/opt/piper \
    PORT=8080

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg wget ca-certificates tar \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY scripts/bundle_piper.sh /app/scripts/bundle_piper.sh
RUN bash /app/scripts/bundle_piper.sh

COPY . /app

EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
