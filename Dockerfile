FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home devlens
USER devlens
EXPOSE 8000
CMD ["uvicorn", "devlens.app.api:app", "--host", "0.0.0.0", "--port", "8000"]
