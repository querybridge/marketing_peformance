from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.index, name="index"),
    path(
        "drill/brand/<int:brand_id>/",
        views.drill_brand,
        name="drill_brand",
    ),
    path(
        "drill/source/<int:brand_id>/<int:source_id>/",
        views.drill_source,
        name="drill_source",
    ),
    path(
        "drill/type/<int:brand_id>/<int:source_id>/<int:type_id>/",
        views.drill_type,
        name="drill_type",
    ),
    path("data-dictionary/", views.data_dictionary, name="data_dictionary"),
    path("alert-spec/", views.alert_spec, name="alert_spec"),
    path("budgets/", views.budgets, name="budgets"),
    path("export/pdf/", views.export_pdf, name="export_pdf"),
    path("verticals/", views.verticals, name="verticals"),
    path("brands/", views.brands, name="brands"),
    path("upload/", views.upload_csv, name="upload_csv"),
    path("revenue/upload/", views.upload_revenue, name="upload_revenue"),
    path("campaigns/match/", views.match_campaigns, name="match_campaigns"),
    path("sites/upload/", views.upload_site_mapping, name="upload_site_mapping"),
    path("help/", views.help_page, name="help"),
    path("optimization/", views.weekly_optimization, name="optimization"),
    path("optimization/export/", views.export_optimization_xlsx, name="optimization_export"),
    path("scoring/", views.scoring_config, name="scoring_config"),
    path("users/", views.add_user, name="add_user"),
]
