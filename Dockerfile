FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLCSIM_DATA_DIR=/var/lib/plc-sim

WORKDIR /opt/plc-sim

COPY PLC-Sim/ /opt/plc-sim/
RUN python -m pip install --no-cache-dir . \
    && groupadd --gid 10001 plc-sim \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /var/lib/plc-sim plc-sim \
    && mkdir -p /var/lib/plc-sim \
    && chown -R 10001:10001 /var/lib/plc-sim

USER 10001:10001

EXPOSE 4855 4856

ENTRYPOINT ["plc-sim"]
CMD ["iccas", "--station", "postprocessing", "--host", "0.0.0.0", "--timing-mode", "process", "--time-scale", "60", "--seed-process-recipe", "--filter-extensions", "--device-model", "--device-state-dir", "/var/lib/plc-sim/iccas-devices"]
