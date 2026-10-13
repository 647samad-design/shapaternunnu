from django.urls import path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerSplitView

from . import views

app_name = "api"

urlpatterns = [
    path("", views.ApiRoot.as_view(), name="root"),
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path("docs/", SpectacularSwaggerSplitView.as_view(url_name="api:schema"), name="docs"),

    # Catalog (public)
    path("categories/", views.CategoryList.as_view(), name="categories"),
    path("products/", views.ProductList.as_view(), name="products"),
    path("products/<int:pk>/", views.ProductDetail.as_view(), name="product"),
    path("products/<int:pk>/reviews/", views.ProductReviews.as_view(), name="product-reviews"),
    path("sellers/<int:pk>/", views.SellerDetail.as_view(), name="seller-public"),

    # Sign-in
    path("auth/token/", views.AuthToken.as_view(), name="token"),
    path("auth/logout/", views.AuthTokenRevoke.as_view(), name="logout"),

    # Customer account
    path("me/", views.Me.as_view(), name="me"),
    path("orders/", views.OrderList.as_view(), name="orders"),
    path("orders/<int:pk>/", views.OrderDetail.as_view(), name="order"),
    path("wishlist/", views.WishlistView.as_view(), name="wishlist"),
    path("wishlist/<int:product_id>/", views.WishlistItem.as_view(), name="wishlist-item"),

    # Seller Center
    path("seller/", views.SellerAccountView.as_view(), name="seller"),
    path("seller/products/", views.SellerProductList.as_view(), name="seller-products"),
    path("seller/products/<int:pk>/", views.SellerProductDetail.as_view(), name="seller-product"),
    path("seller/orders/", views.SellerOrderList.as_view(), name="seller-orders"),
    path("seller/orders/<int:pk>/", views.SellerOrderDetail.as_view(), name="seller-order"),
    path("seller/orders/<int:pk>/fulfil/", views.SellerOrderFulfil.as_view(), name="seller-order-fulfil"),
]
