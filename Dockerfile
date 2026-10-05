# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

COPY fits_cutout/ fits_cutout/
COPY tests/ tests/
COPY verify/ verify/

# Build step: byte-compile every source file so a syntax error fails the
# image build instead of surfacing at runtime.
RUN python -m compileall -q fits_cutout tests verify

EXPOSE 8000

CMD ["python", "-m", "fits_cutout.server"]
