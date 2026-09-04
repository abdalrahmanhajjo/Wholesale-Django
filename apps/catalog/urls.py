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
    path("products/", views.ProductListView.as_view(), name="product_list"),
    path("products/new/", views.ProductCreateView.as_view(), name="product_create"),
    path("products/<int:pk>/", views.ProductDetailView.as_view(), name="product_detail"),
    path("products/<int:pk>/edit/", views.ProductUpdateView.as_view(), name="product_edit"),
    path(
        "products/<int:product_pk>/prices/new/",
        views.ProductPriceCreateView.as_view(),
        name="price_create",
    ),
    path("prices/<int:pk>/edit/", views.ProductPriceUpdateView.as_view(), name="price_edit"),
    path(
        "prices/<int:pk>/delete/",
        views.ProductPriceDeleteView.as_view(),
        name="price_delete",
    ),
]
