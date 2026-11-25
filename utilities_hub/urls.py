from django.urls import path

from hub import views

urlpatterns = [
    path("", views.overview, name="overview"),
    path("energy/", views.energy_view, name="energy"),
    path("water/", views.water_view, name="water"),
    path("transformers/", views.transformers_view, name="transformers"),
    path("transformer/<str:asset_id>/", views.transformer_view, name="transformer"),
    path("solar/", views.solar_view, name="solar"),
    path("solar/block/<str:inv_id>/", views.solar_block_view, name="solar_block"),
    path("api/v1/solar", views.api_solar, name="api_solar"),
    path("api/v1/transformers", views.api_transformers, name="api_transformers"),
    path("asset/<str:asset_id>/", views.asset_view, name="asset"),
    path("alerts/", views.alerts_view, name="alerts"),
    path("meters/", views.meters_view, name="meters"),
    path("meter/<str:meter_id>/", views.meter_view, name="meter"),
    path("api/v1/meters", views.api_meters, name="api_meters"),
    path("api/v1/meters/<str:meter_id>", views.api_meter, name="api_meter"),
    path("ai/", views.ai_view, name="ai"),
    path("fragment/evidence/", views.evidence, name="evidence"),
    path("ai/brief.json", views.brief_json, name="brief_json"),
    path("ai/ask.json", views.ask_json, name="ask_json"),
    path("api/", views.api_docs, name="api_docs"),
    path("api/v1/summary", views.api_summary, name="api_summary"),
    path("api/v1/assets", views.api_assets, name="api_assets"),
    path("api/v1/findings", views.api_findings, name="api_findings"),
    path("api/v1/assets/<str:asset_id>/series", views.api_series, name="api_series"),
]
