FROM mcr.microsoft.com/playwright/python:v1.64.1-noble
WORKDIR /app
RUN pip install --no-cache-dir playwright==1.64.1
COPY location_context.py work_coordinator.py vercel_provider.py satsuma_provider.py provider_comparison.py connector.py api_server.py search_cache.py ingestion.py middleware.html openapi.json swagger.html android-contract.md /app/
ENV PYTHONUNBUFFERED=1
CMD ["python", "api_server.py"]
