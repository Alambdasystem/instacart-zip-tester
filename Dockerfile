FROM mcr.microsoft.com/playwright/python:v1.64.1-noble
WORKDIR /app
RUN pip install --no-cache-dir playwright==1.64.1
COPY connector.py api_server.py search_cache.py middleware.html /app/
ENV PYTHONUNBUFFERED=1
CMD ["python", "api_server.py"]
