"""Catalog routes: units, categories and products."""

from django.urls import path

from apps.catalog import views

app_name = "catalog"

urlpatterns = [
    path("units/", views.UnitOfMeasureListView.as_view(), name="unit_list"),
    path("units/new/", views.UnitOfMeasureCreateView.as_view(), name="unit_create"),
    path("units/<int:pk>/edit/", views.UnitOfMeasureUpdateView.as_view(), name="unit_edit"),
    path("categories/", views.ProductCategoryListView.as_view(), name="category_list"),
    path(
        "categories/new/",
        views.ProductCategoryCreateView.as_view(),
        name="category_create",
    ),
    path(
        "categories/<int:pk>/edit/",
        views.ProductCategoryUpdateView.as_view(),
        name="category_edit",
    ),
]
