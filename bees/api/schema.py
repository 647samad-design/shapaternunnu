from drf_spectacular.openapi import AutoSchema


class ApiSchema(AutoSchema):
    """Groups the endpoints in the documentation."""
    GROUPS = [("/seller/", "Seller Center"), ("/auth/", "Sign in"), ("/me/", "Account"), ("/orders/", "Account"),
              ("/wishlist/", "Account"), ("/products/", "Catalog"), ("/categories/", "Catalog"), ("/sellers/", "Catalog")]

    def get_tags(self):
        for part, tag in self.GROUPS:
            if part in self.path:
                return [tag]
        return ["Overview"]
