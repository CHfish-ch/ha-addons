FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir \
      requests==2.32.3 numpy==2.1.3 h5py==3.12.1 paho-mqtt==2.1.0 \
      astral==3.2

COPY radar.py forecast.py logic.py shade.py events.py run.py version.py \
     irradiance.py solar.py /app/

LABEL io.hass.version="1.7.3" \
      io.hass.type="addon" \
      io.hass.arch="aarch64|amd64"

CMD ["python3", "/app/run.py"]
