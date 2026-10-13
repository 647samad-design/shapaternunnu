from rest_framework.pagination import PageNumberPagination


class Pagination(PageNumberPagination):
    """?page=2&page_size=50 (24 by default, 100 at most)."""
    page_size = 24
    page_size_query_param = "page_size"
    max_page_size = 100
