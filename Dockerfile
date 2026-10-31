FROM mcr.microsoft.com/playwright/python:v1.64.1-noble
WORKDIR /app
RUN pip install --no-cache-dir playwright==1.64.1
COPY performance_probe.py offer_store.py job_store.py search_service.py search_jobs.py cancellation.py isolated_runner.py browser_worker.py provider_session.py pagination.py operator_access.py operator_recover.py migration_validation.py deadline.py page_state.py observation.py direct_http.py telemetry.py provider_registry.py result_store.py location_context.py work_coordinator.py vercel_provider.py satsuma_provider.py provider_comparison.py connector.py api_server.py search_cache.py ingestion.py middleware.html openapi.json swagger.html android-contract.md /app/
ENV PYTHONUNBUFFERED=1
CMD ["python", "api_server.py"]
