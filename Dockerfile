FROM python:3.12-slim

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV ROADS_GEOJSON=/srv/app/app/data/roads.geojson \
    DB_PATH=/data/mapmatch.db \
    MATCH_RADIUS_M=50 \
    HMM_SIGMA_M=15 \
    HMM_BETA_M=30

VOLUME /data
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
